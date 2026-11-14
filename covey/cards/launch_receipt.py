"""The card a submitted launch form becomes: its immutable topic configuration."""
from __future__ import annotations

from pathlib import Path

from covey.cards.elements import markdown_panel, md, note, panel_header, redaction_tags, styled_panel
from covey.core.workspaces import RUN_MODES
from covey.core.text import safe_text
from covey.core.defaults import DEFAULT_WORK_MODE, WORK_MODES, work_mode_tag


def build_receipt_card(selection):
    model = safe_text(str(selection['model_label']), 180)
    topic = safe_text(str(selection.get('topic') or ''), 20)
    effort = budget_and_mode(selection)
    directory = safe_text(str(selection['workdir']), 1000)
    name = safe_text(Path(str(selection['workdir'])).name or '/', 100)
    direct = selection.get('run_mode', 'direct') == 'direct'
    branch = '' if direct else ' · 分支 ' + safe_text(str(selection.get('branch') or ''), 120)
    # What the topic runs in the title bar, the run mode and 敏感词脱敏 as its tags; the workspace's name, its
    # full path one tap away. A model that can redact says 开 or 关 (redaction_tags); a receipt from before says 开 only.
    tags = [(RUN_MODES['direct'], 'neutral') if direct else (RUN_MODES['worktree'], 'wathet')] + redaction_tags(selection.get('redaction'))
    head = panel_header('submitted', '已提交', ' · '.join(filter(None, [f'话题 {topic}' if topic else '', model, effort])),
                        tags, color='green')
    return styled_panel(head, [
        md(f'工作区 **{name}**{branch}'),
        markdown_panel('workspace_path', '完整工作区路径', directory),
    ], receipt_summary(selection))


def budget_and_mode(selection):
    """思考预算 and, for a model with work modes, 工作模式, named as the launch card's summary line names them
    (思考预算 xhigh · 工作模式 Adaptive); a receipt from before work modes were recorded names a mode only when it
    is not the default."""
    tag = work_mode_tag(selection.get('work_mode'))
    if not tag and selection.get('work_modes'):
        tag = WORK_MODES[DEFAULT_WORK_MODE][0]
    return '思考预算 ' + safe_text(str(selection['effort'] or '模型默认'), 40) + (' · 工作模式 ' + tag if tag else '')


def receipt_summary(selection):
    """The chat list's preview of the receipt, the first reply under every topic: what the topic runs
    (已提交 · Codex Acme GPT-6 Astra · xhigh · algo-agent-devskills), not 已提交 · 本话题配置已固定 on all of them."""
    model = str(selection['model_label']).replace(' · ', ' ')
    name = Path(str(selection['workdir'])).name or '/'
    return safe_text(' · '.join(['已提交', model, budget_and_mode(selection), name]), 200)


def build_preparing_card(plan):
    """Shown from the start click until the topic's worktree exists; it has nothing to click."""
    model = safe_text(str(plan['label']), 180)
    topic = safe_text(str(plan.get('topic') or ''), 20)
    effort = budget_and_mode(plan)
    workspace = safe_text(str(plan['workspace']), 100)
    head = panel_header('preparing', '正在创建独立 worktree…',
                        ' · '.join(filter(None, [f'话题 {topic}' if topic else '', model, effort])))
    return styled_panel(head, [
        md(f'工作区 **{workspace}** · 从当前提交新建分支'),
        note('通常需要几秒到十几秒。完成后此卡变为“已提交”，任务自动开始，无需再次点击。'),
    ], '正在创建独立 worktree · 任务即将开始')
