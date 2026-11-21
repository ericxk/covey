"""Task-card button kinds the unified bot accepts, in one table.

A click whose ``kind`` is missing here is dropped as a retired card (baa4a7b lost
clicks that way), so a new button kind is added here first, with the BridgeApp component method
that takes it (``handler``: app/app.py dispatches a topic's click by it). Launch forms,
pickers, the workspace manager, 换配置 (app/harness_switch.py) and 回退
(app/rewind.py) are matched before this table and are not in it. The 代答设置 card's kinds, the
重新发送 card's, the 代发草稿 card's, 群管理's, the 脱敏 tab's (the retired 敏感词看板's 换口令 too), the 管家 settings card's, the 设置 card's tabs, 手上的事's
这些都不用回答了, 飞书应用待补's, the welcome card's, a group's menu card and the shared session's control are here but never reach a task, so
they name no handler: the Bridge handles them before topic routing (app/proxy_panel.py, app/proxy_resend.py,
app/owner_send.py, app/group_manager.py, app/redaction_panel.py, app/butler_panel.py, app/settings_hub.py,
app/overview.py, app/digest.py, app/app_setup.py, app/welcome.py, app/group_menu.py, app/standing_answers.py). The enabled extensions' kinds (core/extensions.py CARD_KINDS) are
added below, bot_only, their features claiming the clicks.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from covey.cards.launch_config import SUBMIT_PREFIX
from covey.cards.project_proposal import LABELS as tidy_labels
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
    # A task that did not succeed (core/failure.py): a read-only turn that looks into why, and the same in smaller
    # steps. Each queues a turn of the topic.
    "bridge_job_diagnose": CardAction("看原因", bot_only=True, handler="submission.follow_up"),
    "bridge_job_split": CardAction("拆小再试", bot_only=True, handler="submission.follow_up"),
    "bridge_fork": CardAction("侧聊或分叉", bot_only=True, handler=RETIRED),  # Off the cards.
    "bridge_compact": CardAction("压缩上下文", bot_only=True, handler=RETIRED),  # Off the cards as well.
    "bridge_turn_export": CardAction("下载请求与结果", bot_only=True, handler=EXPORT),  # Packs in a thread of the Bridge.
    # 看看 under the status line of a turn that may be stuck (cards/task_status.py LOOK_KIND): the Bridge looks at the task
    # runner from outside (app/stuck_check.py), so never forwarded to it; it starts no task.
    "bridge_job_look": CardAction("看看卡住的任务", bot_only=True, handler="stuck_check.look_action"),
    "bridge_steer": CardAction("补充当前任务", handler=QUESTIONS),
    # The 派发卡's buttons (cards/dispatch_card.py): a topic's click like the job's own, not bot_only: with a task
    # process alive the click goes to it (it carries the job's id), as 补充 and 停止 on the task card do.
    "bridge_dispatch_steer": CardAction("补充派出去的任务", handler="steering.dispatch_steer"),
    "bridge_dispatch_stop": CardAction("停止派出去的任务", handler="controls.dispatch_stop"),
    # 不对 on the memory lines of an answer card (cards/answer_card.py memory_element, COVEY_MEMORY=1): takes a line back, starts no task.
    "bridge_memory_wrong": CardAction("撤销刚记下的记忆", handler="controls.memory_wrong"),  # COVEY_MEMORY=1: starts no task.
    # The 专项卡's buttons (cards/project_card.py): the card's message routes to the project's lead topic, so a click is
    # that topic's. Not bot_only: none starts a task (补充 opens the form of the lead's running turn, and says so when it has
    # none), and the value names a project, not a job, so the Bridge takes it in any case (runner_ipc.forward_card_action).
    "bridge_project_steer": CardAction("补充专项", handler="steering.project_steer"),
    "bridge_project_stop": CardAction("停止专项", handler="controls.project_stop"),
    "bridge_project_downgrade": CardAction("不要专项", handler="controls.project_downgrade"),
    "bridge_project_mem_del": CardAction("删专项记忆", handler="controls.project_mem_del"),
    "bridge_project_lib_del": CardAction("删专项资料", handler="controls.project_lib_del"),
    "bridge_project_triage_wrong": CardAction("纠正分流", handler="controls.project_triage_wrong"),  # COVEY_PROJECT_TRIAGE=1
    "bridge_project_triage_fix": CardAction("提交纠正", bot_only=True, handler="controls.project_triage_fix"),  # Queues a message in the member topic.
    # The pause card's buttons (COVEY_PROJECT_ASK=1, cards/project_card.py ask_card): 继续 and 收尾 start the project again and
    # wake its lead (a turn of the bot's own schedule: bot_only, never forwarded to a task runner); 停 stops tasks as
    # 停止专项 does, which is not bot_only. The value names a project like the 专项卡's.
    "bridge_project_ask_continue": CardAction("专项停住后继续", bot_only=True, handler="controls.project_ask"),
    "bridge_project_ask_finish": CardAction("专项停住后收尾", bot_only=True, handler="controls.project_ask"),
    "bridge_project_ask_stop": CardAction("专项停住后停下", handler="controls.project_ask"),
    "bridge_steer_status": CardAction("检查送达状态", handler=QUESTIONS),
    "bridge_steer_requeue": CardAction("重新排队执行", bot_only=True, handler=QUESTIONS),
    "bridge_steer_withdraw": CardAction("撤回补充", handler=QUESTIONS),
    "bridge_steer_interrupt": CardAction("打断命令读补充", handler=QUESTIONS),
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
    "bridge_proxy_model": CardAction("选择代答模型"),  # The select of a card drawn before the rows.
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
    # The 代发草稿 card (app/owner_send.py): a topic's message the owner sends as themselves once they confirm it.
    "bridge_owner_send": CardAction("以你的身份发送"),
    "bridge_owner_send_drop": CardAction("不发代发草稿"),
    "bridge_owner_send_pick": CardAction("选代发收件人"),
    # The 设置 card's 设备 tab (app/node_panel.py, COVEY_NODE_LISTEN): handled before topic routing.
    "bridge_nodes_add": CardAction("添加设备"),
    "bridge_nodes_remove": CardAction("移除设备"),
    "bridge_nodes_refresh": CardAction("刷新设备"),
    "bridge_group_switch": CardAction("启用或停用群"),
    "bridge_group_page": CardAction("翻页"),
    "bridge_group_view": CardAction("打开群的免@设置"),
    "bridge_group_free": CardAction("设置群的免@"),
    "bridge_group_member": CardAction("选择免@的群成员"),
    # The confirmation card the owner's first @ in a group sends to his private chat: 启用 / 先不启用 (app/group_manager.py).
    "bridge_group_confirm": CardAction("确认是否启用群"),
    # The 设置 card's 脱敏 tab (cards/redaction_panel.py KINDS).
    "bridge_dashboard_rotate": CardAction("换口令"),  # Retired with the web dashboard: a click says it went.
    "bridge_redaction_page": CardAction("切换脱敏页面"),  # Retired: a click draws the card again.
    "bridge_redaction_list": CardAction("翻脱敏词表的页"),
    "bridge_redaction_open": CardAction("展开脱敏的试一下或上游地址"),
    "bridge_redaction_find": CardAction("查找敏感词"),
    "bridge_redaction_add": CardAction("加入敏感词"),
    "bridge_redaction_remove": CardAction("批量删除敏感词"),
    "bridge_redaction_term": CardAction("修改或删除敏感词"),
    "bridge_redaction_edit": CardAction("修改敏感词"),  # Retired as well.
    "bridge_redaction_undo": CardAction("撤销删除敏感词"),
    "bridge_redaction_switch": CardAction("切换脱敏开关"),
    "bridge_redaction_upstream": CardAction("设置上游地址"),
    "bridge_redaction_probe": CardAction("探测上游地址"),
    "bridge_redaction_preview": CardAction("试一下脱敏"),
    # The 管家 settings card (cards/butler_panel.py KINDS).
    "bridge_main_enable": CardAction("开关管家"),
    "bridge_main_mode": CardAction("选择私聊模式"),
    "bridge_main_start_row": CardAction("选择直接开工"),
    "bridge_main_harness": CardAction("选择管家 Harness"),
    "bridge_main_workspace": CardAction("选择管家工作区"),
    "bridge_main_model": CardAction("选择管家模型"),
    "bridge_main_effort": CardAction("选择管家思考预算"),
    "bridge_main_provider": CardAction("选择管家供应商"),
    "bridge_main_redaction": CardAction("选择管家脱敏"),
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
    # The 看板's 刷新 and 记为看过 (cards/board.py KINDS): the Bridge redraws the card (app/digest.py Digest.claim).
    "bridge_board_refresh": CardAction("刷新看板"),
    "bridge_board_seen": CardAction("记为看过"),
    # 飞书应用待补 (cards/app_setup.py): the owner's, in the private chat (app/app_setup.py).
    "bridge_app_setup": CardAction("补齐飞书应用配置"),
    "bridge_app_setup_skip": CardAction("先不补飞书应用配置"),
    # 发这句 on the welcome card (cards/welcome.py KIND): the owner's, taken before topic routing (app/welcome.py); bot_only:
    # its example becomes a message of the owner's, which starts a task.
    "bridge_welcome_try": CardAction("发欢迎卡上的示例", bot_only=True),
    # The menu card of a group with a butler (app/group_menu.py KIND).
    "bridge_group_menu": CardAction("群里的菜单"),
    # 以后这类按推荐走 under a question card and its own card's 记下 / 改一下 / 算了 (app/standing_answers.py): the Bridge
    # takes them before topic routing (PANELS); 记下 answers the question through its own submit path.
    "bridge_question_standing": CardAction("以后这类按推荐走"),
    "bridge_standing_save": CardAction("记下原则"),
    "bridge_standing_edit": CardAction("改一下原则"),
    "bridge_standing_cancel": CardAction("不记原则"),
}
# 整理专项's cards (cards/project_proposal.py KINDS, COVEY_PROJECT_TIDY): the proposal card, its rename form, the 整理专项 card,
# 移出 on a topic's card and the 设置 card's 整理专项. The Bridge takes them before topic routing (app/project_tidy.py claim).
ACTIONS.update({kind: CardAction(label) for kind, label in tidy_labels.items()})
ACTIONS.update({kind: CardAction(label, bot_only=True) for kind, label in extensions.card_kinds().items()})
BOT_ONLY = frozenset(kind for kind, action in ACTIONS.items() if action.bot_only)
# What a click on a retired button gets instead (app/task_controls.py retired_action).
RETIRED_TEXT = {
    "bridge_fork": "💬 侧聊和 🔀 分叉已下线：要从这个话题分出新话题，在私聊里跟管家说。",
    "bridge_compact": "📦 压缩上下文已下线：会话快到模型窗口时，Claude Code 会自己压缩。",
}
# compat: cards sent by a feature that was taken out: (kind, the prefix of its forms' button names) -> what a click gets
# (a card kind not in ACTIONS is otherwise dropped without a word). Paseo (covey_paseo) was taken out.
MOVED_CARDS = {("bridge_paseo", "bridge_paseo_"): "Paseo 已下线，卡片上的操作没有执行。"}


def moved_card_text(event) -> str:
    """The notice for a click on a card of a feature that was taken out, else ''."""
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
    string of one; {} when none is set, None when it is not JSON or not an object."""
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
