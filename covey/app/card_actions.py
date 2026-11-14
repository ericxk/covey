"""Task-card button kinds the unified bot accepts, in one table.

A click whose ``kind`` is missing here is dropped as a retired card (baa4a7b lost
clicks that way), so a new button kind is added here first, with the BridgeApp component method
that takes it (``handler``: app/app.py dispatches a topic's click by it). Launch forms,
pickers, the workspace manager, 换配置 (app/harness_switch.py) and 回退
(app/rewind.py) are matched before this table and are not in it. The 代答设置 card's kinds, the
重新发送 card's, 群管理's, 敏感词看板's 换口令, the 脱敏 tab's, the 主线程 settings card's, the 设置 card's tabs, 手上的事's
这些都不用回答了, 飞书应用待补's, a group's menu card and the shared session's control are here but never reach a task, so
they name no handler: the Bridge handles them before topic routing (app/proxy_panel.py, app/proxy_resend.py,
app/group_manager.py, app/dashboard.py, app/redaction_panel.py, app/main_thread_panel.py, app/settings_hub.py,
app/overview.py, app/app_setup.py, app/group_menu.py). The enabled extensions' kinds (core/extensions.py CARD_KINDS) are
added below, bot_only, their features claiming the clicks.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from covey.cards.launch_config import SUBMIT_PREFIX
from covey.core import extensions


@dataclass(frozen=True)
class CardAction:
    label: str  # Names the action in the "…未完成" reply.
    bot_only: bool = False  # Starts a new job, or acts on the bot's own schedule: never forwarded to a task runner.
    handler: str = ""  # "component.method" of the BridgeApp that takes a topic's click; "" before topic routing.


# A component method takes (ctx, event) and returns True once it has taken the click.
QUESTIONS = "interaction.interaction_action"  # Answers, questions' states, supplements, held notes, old pages.
ARTIFACTS = "artifacts.artifact_action"
RETIRED = "controls.retired_action"  # A button taken off the cards, on a card sent before: what replaces it.
RETRIES = "controls.task_action"
EXPORT = "turn_export.export_action"


ACTIONS = {
    "bridge_job_stop": CardAction("停止任务", handler="controls.stop_action"),
    "bridge_job_retry": CardAction("重试任务", bot_only=True, handler=RETRIES),
    "bridge_job_retry_cancel": CardAction("停止自动重试", bot_only=True, handler=RETRIES),
    "bridge_fork": CardAction("侧聊或分叉", bot_only=True, handler=RETIRED),  # Off the cards since 2026-11-08.
    "bridge_compact": CardAction("压缩上下文", bot_only=True, handler=RETIRED),  # Off the cards since 2026-11-08.
    "bridge_turn_export": CardAction("下载请求与结果", bot_only=True, handler=EXPORT),  # Packs in a thread of the Bridge.
    "bridge_steer": CardAction("补充当前任务", handler=QUESTIONS),
    "bridge_steer_status": CardAction("检查送达状态", handler=QUESTIONS),
    "bridge_steer_requeue": CardAction("重新排队执行", bot_only=True, handler=QUESTIONS),
    "bridge_steer_withdraw": CardAction("撤回补充", handler=QUESTIONS),
    "bridge_note": CardAction("处理暂存消息", bot_only=True, handler=QUESTIONS),
    "bridge_question": CardAction("提交回答", handler=QUESTIONS),
    # Handled by the Bridge, which hands the answer on to a runner that still waits for it (forward_answer);
    # bot_only: it queues a follow-up task.
    "bridge_question_skip": CardAction("只发补充说明", bot_only=True, handler=QUESTIONS),
    "bridge_question_resume": CardAction("核查已保存回答", handler=QUESTIONS),
    "bridge_question_status": CardAction("查看回答状态", handler=QUESTIONS),
    "bridge_trace": CardAction("查看执行过程", handler=QUESTIONS),
    "bridge_artifact_send": CardAction("发送产物", handler=ARTIFACTS),
    "bridge_artifact_page": CardAction("查看产物", handler=ARTIFACTS),
    "bridge_artifact_upload": CardAction("上传产物", handler=ARTIFACTS),
    "bridge_proxy_enable": CardAction("开启代答"),
    "bridge_proxy_disable": CardAction("关闭代答"),
    "bridge_proxy_harness": CardAction("选择代答 Harness"),
    "bridge_proxy_provider": CardAction("选择代答供应商"),
    "bridge_proxy_tier_model": CardAction("选择代答模型"),
    "bridge_proxy_tier_budget": CardAction("选择代答思考预算"),
    "bridge_proxy_model": CardAction("选择代答模型"),  # The select of a card drawn before the rows (2026-11-10).
    "bridge_proxy_budget": CardAction("选择代答思考预算"),
    "bridge_proxy_find": CardAction("查找联系人"),
    "bridge_proxy_add": CardAction("加入白名单"),
    "bridge_proxy_add_all": CardAction("全部添加"),
    "bridge_proxy_members": CardAction("读取群成员"),
    "bridge_proxy_members_add": CardAction("添加群成员"),
    "bridge_proxy_members_cancel": CardAction("取消添加群成员"),
    "bridge_proxy_pause": CardAction("暂停代答"),
    "bridge_proxy_resume": CardAction("恢复代答"),
    "bridge_proxy_remove": CardAction("移出白名单"),
    "bridge_proxy_source_pause": CardAction("暂停群成员代答"),
    "bridge_proxy_source_resume": CardAction("恢复群成员代答"),
    "bridge_proxy_source_remove": CardAction("移出群成员"),
    "bridge_proxy_source_open": CardAction("逐个管理群成员"),
    "bridge_proxy_page": CardAction("翻页"),
    "bridge_proxy_login": CardAction("网页端登录"),
    "bridge_proxy_refresh": CardAction("刷新代答"),
    "bridge_proxy_resend": CardAction("重新发送代答"),
    "bridge_proxy_suggest": CardAction("按建议回复"),  # The 重新发送 card of a turn that would not answer (U5).
    "bridge_group_switch": CardAction("启用或停用群"),
    "bridge_group_page": CardAction("翻页"),
    "bridge_group_view": CardAction("打开群的免@设置"),
    "bridge_group_free": CardAction("设置群的免@"),
    "bridge_group_member": CardAction("选择免@的群成员"),
    "bridge_dashboard_rotate": CardAction("换口令"),
    # The 设置 card's 脱敏 tab (cards/redaction_panel.py KINDS).
    "bridge_redaction_page": CardAction("切换脱敏页面"),  # Retired 2026-11-14: a click draws the card again.
    "bridge_redaction_list": CardAction("翻脱敏词表的页"),
    "bridge_redaction_open": CardAction("展开脱敏的试一下或上游地址"),
    "bridge_redaction_find": CardAction("查找敏感词"),
    "bridge_redaction_add": CardAction("加入敏感词"),
    "bridge_redaction_term": CardAction("修改或删除敏感词"),
    "bridge_redaction_edit": CardAction("修改敏感词"),  # Retired 2026-11-14 as well.
    "bridge_redaction_undo": CardAction("撤销删除敏感词"),
    "bridge_redaction_switch": CardAction("切换脱敏开关"),
    "bridge_redaction_upstream": CardAction("设置上游地址"),
    "bridge_redaction_probe": CardAction("探测上游地址"),
    "bridge_redaction_preview": CardAction("试一下脱敏"),
    # The 主线程 settings card (cards/main_thread_panel.py KINDS).
    "bridge_main_enable": CardAction("开关主线程"),
    "bridge_main_model": CardAction("选择主线程模型"),
    "bridge_main_effort": CardAction("选择主线程思考预算"),
    "bridge_main_provider": CardAction("选择主线程供应商"),
    "bridge_main_redaction": CardAction("选择主线程脱敏"),
    "bridge_main_compact": CardAction("选择压缩阈值"),
    "bridge_main_topic_harness": CardAction("选择新话题 Harness"),
    "bridge_main_topic_provider": CardAction("选择新话题供应商"),
    "bridge_main_topic_model": CardAction("选择新话题模型"),
    "bridge_main_topic_effort": CardAction("选择新话题思考预算"),
    "bridge_main_topic_run_mode": CardAction("选择新话题运行方式"),
    "bridge_main_topic_redaction": CardAction("选择新话题脱敏"),
    "bridge_main_topic_workspace": CardAction("选择新话题工作区"),
    "bridge_main_subagents": CardAction("选择子 Agent 模型"),
    "bridge_main_start": CardAction("设置直接开工"),
    "bridge_main_untrust": CardAction("删除记住的工作区"),
    "bridge_main_notes": CardAction("保存工作区用途"),
    # The 设置 card's tab row (cards/settings_hub.py KINDS).
    "bridge_settings_tab": CardAction("切换设置标签"),
    # 手上的事's line of old questions (cards/overview.py DISMISS): only the board's record changes.
    "bridge_overview_dismiss": CardAction("撤下看板"),
    # 飞书应用待补 (cards/app_setup.py): the owner's, in the private chat (app/app_setup.py).
    "bridge_app_setup": CardAction("补齐飞书应用配置"),
    "bridge_app_setup_skip": CardAction("先不补飞书应用配置"),
    # The menu card of a group with a main thread (app/group_menu.py KIND).
    "bridge_group_menu": CardAction("群里的菜单"),
}
ACTIONS.update({kind: CardAction(label, bot_only=True) for kind, label in extensions.card_kinds().items()})
BOT_ONLY = frozenset(kind for kind, action in ACTIONS.items() if action.bot_only)
# What a click on a retired button gets instead (app/task_controls.py retired_action).
RETIRED_TEXT = {
    "bridge_fork": "💬 侧聊和 🔀 分叉已下线：要从这个话题分出新话题，在私聊里跟主线程说。",
    "bridge_compact": "📦 压缩上下文已下线：会话快到模型窗口时，Claude Code 会自己压缩。",
}
# compat: cards sent by a feature that moved into an extension: (kind, the prefix of its forms' button names) -> what a click
# gets while the extension is not enabled (a card kind not in ACTIONS is otherwise dropped without a word).
MOVED_CARDS = {("bridge_paseo", "bridge_paseo_"): "这个功能需要 Paseo 扩展（covey_paseo），当前没有启用，卡片上的操作没有执行。"}


def moved_card_text(event) -> str:
    """The notice for a click on a card of an extension that is not enabled, else ''."""
    kind, name = action_value(event).get("kind"), str(event.get("action_name") or "")
    for (moved_kind, prefix), text in MOVED_CARDS.items():
        if kind == moved_kind or name.startswith(prefix):
            return text
    return ""


def action_label(kind) -> str:
    action = ACTIONS.get(kind)
    return action.label if action else "卡片操作"


def card_object(event, *names) -> dict | None:
    """The first of the event's ``names`` that is set, as an object: the client sends one or the JSON
    string of one (2026-10-27); {} when none is set, None when it is not JSON or not an object."""
    value = next(filter(None, map(event.get, names)), {})
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, dict) else None


def action_value(event) -> dict:
    """A click's value; {} when it is missing, not JSON or not an object."""
    return card_object(event, "action_value") or {}


def form_fields(event) -> dict | None:
    """A form's fields, from form_value or formValue; {} when there are none, None when they are not
    JSON or not an object."""
    return card_object(event, "form_value", "formValue")


def click_kind(event) -> str:
    """What the log lines call a click: its value's ``kind``; for a form button, which has none, its
    name without the form's token (开始任务 is ``bridge_launch_submit``); "unknown" without either."""
    kind = action_value(event).get("kind")
    if kind:
        return str(kind)[:40]
    name = str(event.get("action_name") or "")
    if name.startswith(SUBMIT_PREFIX):
        return "bridge_launch_submit"
    return re.sub(r"_[0-9a-f]{24}$", "", name.split(":")[0])[:40] or "unknown"
