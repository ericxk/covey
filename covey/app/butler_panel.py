"""The 管家 settings card (docs/butler.md 设置卡): the owner changes the butler's settings in the private
chat. The butler only sends it (the tool ``settings_panel``, TOOLS); it cannot change its settings.

It is the bot menu's 设置 card's tab 配置 (app/settings_hub.py, cards/settings_hub.py CONFIG), one section a use: 新话题
(every new topic's preselection), 管家 (the butler's own, and the private chat mode: 每条直接开一个话题
is saved only after 确定改, the record's view keeping ``confirm`` meanwhile), 独立复核 (app/review.py), 专项负责人 (what a new
project's lead runs on, with COVEY_PROJECTS=1: app/lead_harness.py) and 子 Agent; the
pages 管家, 新话题 and 模型 it took the place of are retired into it. Its 工作区 and 运行方式 rows and the 工作区 tab's 保存用途 are gone
(cards/butler_panel.py OFFLINE_KINDS): an older card's click on one says OFFLINE_NOTICE and changes nothing; the repositories are the 设置 card's
仓库 tab (app/workspace_manager.py; ``pages``: this panel redraws that tab through it for a record still on it) and the web page's.
A card on a retired page (其他, 管家, 新话题, 模型, 工作区) still acts: its rows land on 配置 (PAGE_OF). The tool and, unless COVEY_BUTLER=0, the menu key MENU_KEY send that card on 配置; the chat keeps one 设置
card, a new one recalling the one before (app/bot_menu.py Panels). A click redraws the page of its button (PAGE_OF; the
record's ``tab`` follows). No topic, no job. The 设置 card's record holds the tab's view (revision, notice); a card
sent before there was a 设置 card (gen 286) has its own record, <state>/main-thread-panels/<token>.json, and still
acts. The settings themselves are read from core/butler_settings.py at each draw and written only by
``update``, so every card shows what holds now. Each click changes one field to the state its value names, then
the whole card is drawn again (rule 9). Only the owner acts; anyone else's click is refused on the card.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, NamedTuple

from covey.app import butler_harness
from covey.app.launch_models import available_models
from covey.core.menu_choice import (budget_effort, butler_harnesses, default_entry, entry_of, explicit,
                                    model_provider, models_of, plain_budget, redacts, tiers)
from covey.app.butler_env import reply
from covey.app.butler_model_choice import resolve
from covey.app.menu_panel import MenuPanel, hub_top
from covey.cards.butler_panel import (ASSIST, ASSIST_EFFORT, ASSIST_MODEL, ASSIST_PROVIDER, COMPACT, EFFORT, ENABLE, HARNESS, KINDS, MODE, MODE_CHOICES, MODEL, MODEL_FOLLOW,
                                                    NOTE_LENGTH, OFFLINE_KINDS, OFFLINE_NOTICE, PAGE_OF, PROVIDER, REDACTION,
                                                    RETIRED_KINDS, RETIRED_NOTICE, SUBAGENTS,
                                                    TOPIC_EFFORT, TOPIC_HARNESS, TOPIC_MODEL, TOPIC_PROVIDER, TOPIC_REDACTION,
                                                    CLAUDE, follow_text,
                                                    REVIEW_EFFORT, REVIEW_HARNESS, REVIEW_MODEL, REVIEW_PROVIDER,
                                                    LEAD_EFFORT, LEAD_HARNESS, LEAD_MODEL, LEAD_PROVIDER, MORE_KINDS,
                                                    PART_OF, SECTION, SECTIONS,
                                                    butler_panel_card)
from covey.cards.launch_config import effort_choices
from covey.cards.settings_hub import BUTLER, BUTLER_PAGES, CONFIG, OTHER, REPOS, RETIRED, TOPIC, WORKSPACE
from covey.core import butler, butler_settings, projects
from covey.core.jobs import job_file, read_job_record
from covey.core.butler_settings import SUBAGENTS as SUBAGENT_MODES
from covey.core.state import Unreadable, read_object
from covey.core.workspaces import config_path

FOLDER = 'main-thread-panels'
LOCK = 'main-thread-panel'
MENU_KEY = 'main_thread_settings'  # The bot menu's event key that opens the 设置 card on this tab.
OWNER_ONLY = '⚠️ 只有机器人主人能改管家的设置。'
GONE = '⚠️ 这个选项已不可用，卡片已按最新设置重画。'
UNREADABLE_NOTICE = '⚠️ ' + butler_settings.UNREADABLE + '。'
# Said on the card once the private chat mode changed (docs/butler.md 私聊模式).
MODE_NOTICES = {True: '已改成交给管家：私聊顶层的消息交给管家。',
                False: '已改成每条直接开一个话题；已开的话题照常在各自的话题串里继续。'}
REVIEW = 'review'  # The scope of 独立复核's rows (ROW_CHANGES).
LEAD = 'lead'  # The scope of 专项负责人's rows (ROW_CHANGES).
ASSIST_SCOPE = 'assist'  # The scope of 辅助模型's 供应商, 模型 and 思考预算 rows (ROW_CHANGES).
# Said on the card once another setting was saved (docs/ui.md 5.2: 保存后用一句灰字回执), by the scope of the row
# (ROW_CHANGES), else its page.
SAVED = {BUTLER: '已保存：管家下一轮起照这个来。', TOPIC: '已保存：之后新开的话题照这个来。', WORKSPACE: '已保存。',
         REVIEW: '已保存：之后点的独立复核照这个来。',
         LEAD: '已保存：之后新建的专项，负责人照这个来；已有的专项在专项卡上点「换配置」。'}
# A row whose change waits for the butler to be idle and starts a new session (app/butler.py write_topic).
SAVED_NEW_SESSION = '已保存：管家手上这一轮做完后换过去，开一个新会话，最近的对话会带上。'
SAVED_OF = {HARNESS: SAVED_NEW_SESSION,
            **dict.fromkeys((ASSIST, ASSIST_PROVIDER, ASSIST_MODEL, ASSIST_EFFORT), '已保存：后台活从下一次起照这个来。')}


def workspace_names(state_dir):
    """The saved workspaces' names in their order (core/workspaces.py), read only: [] before there is a list."""
    workspaces = read_object(config_path(Path(state_dir))).get('workspaces')
    return [str(name) for name in workspaces] if isinstance(workspaces, dict) else []


def offered(entry, effort, empty=True):
    """Whether the 思考预算 row of ``entry`` (a menu entry, or None) offers ``effort`` (cards/launch_config.py
    effort_choices: '' where ``empty``, the levels by name)."""
    return effort in dict(effort_choices(entry, empty=empty))


def kept(entry, effort, empty=True):
    """``effort`` where the newly chosen model ``entry`` offers it too; else '' (what the row's empty choice says)
    where ``empty``, else a level of its own (core/menu_choice.py plain_budget)."""
    if entry and offered(entry, effort, empty):
        return effort
    return plain_budget(entry, effort) if entry and not empty else ''


def new_budget(rows, entry):
    """The budget saved with the newly chosen model ``entry`` (kept), as the page saves a 思考预算 option."""
    effort = kept(entry, rows.effort, rows.empty)
    return rows.budget(entry, effort) if effort and entry else effort


class Rows(NamedTuple):
    """A page's Harness, 供应商, 模型 and 思考预算 rows as the row handlers (ROW_CHANGES) read and write them: 私聊默认's
    are the butler's own, 新话题默认's a new topic's."""
    harness: str  # '' none chosen: the 新话题默认 rows under it offer nothing.
    model: str
    effort: str
    empty: bool  # Whether the rows have an empty choice (新话题默认's: what it follows); 管家's name each.
    entry: Callable  # () -> the menu entry whose budgets the 思考预算 row offers (or None).
    budget: Callable  # (entry, option) -> the budget saved for a 思考预算 option.
    save: Callable  # (**fields) -> the settings fields that set them.


def pick_provider(rows, choices, option, state_dir):
    """供应商: a provider with its first model (the model id carries the provider: acme/gpt-6-astra), as on the launch
    card, and the budget where that model offers it too; '' (新话题默认 only) leaves the model to the default."""
    providers = tiers(choices, rows.harness)[1] if rows.harness else {}
    if not rows.harness or option and option not in providers or not option and not rows.empty:
        return None
    if option == model_provider(rows.harness, rows.model, choices):
        return {}
    first = next(iter(models_of(explicit(choices), rows.harness, option)), '') if option else ''
    if option and not first:
        return None
    return rows.save(model=first, effort=new_budget(rows, entry_of(choices, rows.harness, first)))


def pick_model(rows, choices, option, state_dir):
    """模型: any of the Harness's models by name (a card drawn before may list another provider's, or "Default
    (recommended)": no more, T615), the budget where it offers it too; '' only on 新话题默认. The saved one again
    changes nothing."""
    if option == rows.model:
        return {}
    if not rows.harness or option and option not in models_of(explicit(choices), rows.harness) \
            or not option and not rows.empty:
        return None
    return rows.save(model=option, effort=new_budget(rows, entry_of(choices, rows.harness, option)))


def pick_effort(rows, choices, option, state_dir):
    """思考预算: a level by name ('' only on 新话题默认); 推荐 or 模型原生默认 of a card drawn before is no more (T615).
    The saved one again changes nothing."""
    if option == rows.effort:
        return {}
    entry = rows.entry()
    if not offered(entry, option, empty=rows.empty):
        return None
    return rows.save(effort=rows.budget(entry, option))


def pick_harness(rows, choices, option, state_dir):
    """新话题默认's Harness: another one empties the model and budget."""
    if option and option not in tiers(choices, option)[0]:
        return None
    return rows.save(**({'harness': option} if option == rows.harness else {'harness': option, 'model': '', 'effort': ''}))


def pick_redaction(rows, choices, option, state_dir):
    """The butler's 脱敏: on only where its Harness can redact."""
    if option not in ('on', 'off') or option == 'on' and not redacts(choices, rows.harness):
        return None
    return {'redaction': option == 'on'}


def pick_butler_harness(rows, choices, option, state_dir):
    """The butler's Harness: one that can run it (butler_harnesses), and with 脱敏 on one that can redact too (the card
    lists no other: REDACT_FIRST); another one takes its first model by name and that model's budget
    (core/menu_choice.py plain_budget), so the rows say what the butler runs (T615). Claude Code is saved as '', the
    default Harness."""
    if option == rows.harness:
        return {}
    if option not in butler_harnesses(choices, redacting=butler_settings.load(state_dir).redaction):
        return None
    first = next(iter(models_of(explicit(choices), option)), '')
    entry = entry_of(choices, option, first)
    return {'harness': '' if option == CLAUDE else option, 'model': first,
            'effort': budget_effort(entry, plain_budget(entry, rows.effort)) if entry else ''}


def pick_review_harness(rows, choices, option, state_dir):
    """独立复核's Harness: '' picks by itself (app/review.py pick), emptying the rest; another one on the menu takes its
    first model by name and that model's budget, as the butler's (pick_butler_harness)."""
    if option == rows.harness:
        return {}
    if not option:
        return rows.save(harness='', model='', effort='')
    if option not in tiers(choices, option)[0]:
        return None
    first = next(iter(models_of(explicit(choices), option)), '')
    entry = entry_of(choices, option, first)
    return rows.save(harness=option, model=first,
                     effort=budget_effort(entry, plain_budget(entry, rows.effort)) if entry else '')


def pick_lead_harness(rows, choices, option, state_dir):
    """专项负责人's Harness: '' follows the butler, emptying the rest; another one that can run a lead (the butler's list,
    with 脱敏 on only those that redact: the lead keeps the butler's, app/lead_harness.py) takes its first model by name and
    that model's budget, as the butler's (pick_butler_harness)."""
    if option == rows.harness:
        return {}
    if not option:
        return rows.save(harness='', model='', effort='')
    if option not in butler_harnesses(choices, redacting=butler_settings.load(state_dir).redaction):
        return None
    first = next(iter(models_of(explicit(choices), option)), '')
    entry = entry_of(choices, option, first)
    return rows.save(harness=option, model=first,
                     effort=budget_effort(entry, plain_budget(entry, rows.effort)) if entry else '')


def saved_notice(kind):
    """What the card says once a row of ``kind`` was saved (SAVED_OF, else its scope's or its page's SAVED)."""
    scope = ROW_CHANGES.get(kind, (None,))[0]
    return SAVED_OF.get(kind) or SAVED.get(scope) or SAVED.get(PAGE_OF.get(kind), '已保存。')


# A settings row's kind -> (its scope, BUTLER the butler's own, TOPIC a new topic's or bot-wide, REVIEW 独立复核's; what a
# choice on it changes: rows, choices, option, state_dir -> the settings fields, {} for none, None when the choice is
# no longer offered). The three scopes share the 供应商, 模型 and 思考预算 rows'.
ROW_CHANGES = {
    HARNESS: (BUTLER, pick_butler_harness),
    PROVIDER: (BUTLER, pick_provider), MODEL: (BUTLER, pick_model), EFFORT: (BUTLER, pick_effort),
    REDACTION: (BUTLER, pick_redaction),
    COMPACT: (BUTLER, lambda rows, choices, option, state_dir: {'compact_tokens': int(option)} if option.isdigit() else None),
    TOPIC_HARNESS: (TOPIC, pick_harness), TOPIC_PROVIDER: (TOPIC, pick_provider), TOPIC_MODEL: (TOPIC, pick_model),
    TOPIC_EFFORT: (TOPIC, pick_effort),
    TOPIC_REDACTION: (TOPIC, lambda rows, choices, option, state_dir: (
        rows.save(redaction=option == 'on') if option in ('on', 'off') else None)),
    # 子 Agent: bot-wide (backends/claude/subagents.py), not a new topic's default, though on that page.
    SUBAGENTS: (TOPIC, lambda rows, choices, option, state_dir: (
        {'subagents': option} if option in SUBAGENT_MODES else None)),
    REVIEW_HARNESS: (REVIEW, pick_review_harness), REVIEW_PROVIDER: (REVIEW, pick_provider),
    REVIEW_MODEL: (REVIEW, pick_model), REVIEW_EFFORT: (REVIEW, pick_effort),
    LEAD_HARNESS: (LEAD, pick_lead_harness), LEAD_PROVIDER: (LEAD, pick_provider),
    LEAD_MODEL: (LEAD, pick_model), LEAD_EFFORT: (LEAD, pick_effort),
    ASSIST_PROVIDER: (ASSIST_SCOPE, pick_provider), ASSIST_MODEL: (ASSIST_SCOPE, pick_model),
    ASSIST_EFFORT: (ASSIST_SCOPE, pick_effort),
}


def assist_pick(saved, picked, choices):
    """The 辅助模型 a click on its Harness row saves: 自动 and 不用模型 as they are; a Harness other than the one named
    takes its first model by name and that model's budget, as 专项负责人's (pick_lead_harness), so the rows say what the
    background calls run on; the one named already, nothing new."""
    if not picked.harness or picked.off:
        return picked
    if picked.harness == saved.harness and not saved.off:
        return saved
    first = next(iter(models_of(explicit(choices), picked.harness)), '')
    entry = entry_of(choices, picked.harness, first)
    return dataclasses.replace(picked, model=first,
                               effort=budget_effort(entry, plain_budget(entry, saved.effort)) if entry else '')


class ButlerPanel:
    def __init__(self, topics):
        """``topics`` (app/topics.py Topics) gives the model catalog's apps and the messenger and voice."""
        self.topics = topics
        # Its buttons act on its pages and, for an older card, on the retired 工作区 and 其他 (and the 仓库 they land on).
        self.menu = MenuPanel(topics, 'main-thread-panel', FOLDER, LOCK, tab=(*BUTLER_PAGES, REPOS, WORKSPACE, OTHER))
        # Another panel's tab its buttons are on -> tab_card(ctx, token, record) of that panel (app/settings_hub.py).
        self.pages = {}

    def path(self, ctx, token):
        return self.menu.path(ctx, token)

    @staticmethod
    def matches(value):
        return value.get('kind') in KINDS

    def claim(self, ctx, event, raw, owner):
        """The click's outcome when it is a 管家 settings card's, else None (app/unified.py PANELS)."""
        return self.click(ctx, event, raw, owner) if self.matches(raw) else None

    def card(self, ctx, view, tab=CONFIG, chat=''):
        """The card of ``view``: on the 设置 card (``hub`` in it) the page ``tab`` (配置, a retired one as it; another
        panel's: ``pages``), else the card of its own (gen 286). ``chat``: the chat it is in, whose last launch a new topic follows ('' the
        owner's private chat; a personal group opens its own)."""
        tab = RETIRED.get(tab, tab)
        if view.get('hub') and tab in self.pages:
            return self.pages[tab](ctx, view['token'], {'view': view, 'tab': tab})
        from covey.app import review

        # The butler's Harness as it runs now when the settings name none (app/butler_harness.py drawn, never saved).
        settings = butler_harness.drawn(butler_settings.load(ctx.state_dir), self.topics.apps)
        choices = available_models(self.topics, ctx)
        page = tab if view.get('hub') and tab in BUTLER_PAGES else None
        defaults = settings.topic_defaults
        return butler_panel_card(view['token'], view, settings, choices, workspace_names(ctx.state_dir),
                                      top=hub_top(page or CONFIG, view['token'], view), page=page,
                                      follows=self.follows(ctx, defaults, choices, chat),
                                      fallback=self.fallback(ctx, defaults, choices, chat),
                                      butler_on=butler.available() and settings.enabled,
                                      review_on=review.enabled(), butler_allowed=butler.available(),
                                      lead_on=projects.enabled(), assist=self.assist_row(ctx, settings))

    def assist_row(self, ctx, settings):
        """辅助模型's row on 配置 (app/assist.py): its choices, the one saved, what it runs on now."""
        from covey.app import assist
        return {'options': assist.offered(self.topics, state=ctx.state_dir), 'current': assist.current(settings),
                'words': assist.describe(self.topics, state=ctx.state_dir)['words']}

    def resolved(self, ctx, defaults, choices, chat=''):
        """What a topic the butler of ``chat`` opens now runs with over ``defaults`` (app/butler_topics.py resolve,
        that chat's last launch; '' the owner's private chat); None when it cannot say."""
        found = resolve(self.topics, ctx.state_dir, chat or self.topics.runtime.access.owner_chat(), defaults,
                        choices=choices)
        return None if isinstance(found, str) else found

    def fallback(self, ctx, defaults, choices, chat=''):
        """The menu entry a new topic runs with its Harness chosen and its 模型 empty (resolve: the last launch's or
        the Bridge's model), whose budgets the 思考预算 row offers; None with a model or no Harness chosen."""
        if not defaults.harness or defaults.model:
            return None
        found = self.resolved(ctx, defaults, choices, chat)
        return choices.get(found['model_key']) if found and found['harness'] == defaults.harness else None

    def follows(self, ctx, defaults, choices, chat=''):
        """{row key: text} of what each 新话题 row's empty option sets (ACCEPT-325 问题 2): ``defaults`` with the
        row and those under it empty (a click on it empties them: changes), resolved now: '跟上次一样：opus · max',
        '系统默认：high'. A row whose text cannot be said is left out (默认)."""
        empty = {'t_harness': dict(harness='', model='', effort=''), 't_model': dict(model='', effort=''),
                 't_effort': dict(effort='')}
        found = {key: self.resolved(ctx, dataclasses.replace(defaults, **fields), choices, chat)
                 for key, fields in empty.items()}
        texts = {}
        if found['t_harness']:
            whole = found['t_harness']
            sources = whole['sources']
            title = tiers(choices, whole['harness'])[0].get(whole['harness']) or whole['app'].spec.title
            texts['t_harness'] = follow_text([(title, sources['harness']), (whole['label'], sources['model']),
                                              (whole['effort'] or '模型默认', sources['effort'])])
        if found['t_model']:
            model = (found['t_model']['label'], found['t_model']['sources']['model'])
            provider = choices.get(found['t_model']['model_key'], {}).get('provider_title') or ''
            texts['t_model'] = follow_text([model], MODEL_FOLLOW)
            texts['t_provider'] = follow_text([(provider, model[1]), model] if provider else [model])
        if found['t_effort']:
            effort = found['t_effort']
            texts['t_effort'] = follow_text([(effort['effort'] or '模型默认', effort['sources']['effort'])])
        return texts

    # The 设置 card's 管家 tab (app/settings_hub.py): its first view, its record's fields, its card, once shown.
    @staticmethod
    def tab_view(token):
        return {'token': token, 'revision': 1, 'hub': True}

    @staticmethod
    def tab_fields(ctx):
        return {}

    def tab_card(self, ctx, token, record):
        return self.card(ctx, record['view'], record.get('tab', CONFIG), record.get('chat_id') or '')

    def tab_shown(self, ctx, token):
        self.topics.runtime.log(f'main thread panel shown token={token[:8]}')

    def refuse(self, ctx, path, event):
        """OWNER_ONLY on the card ``event`` clicked; whether it was a settings card's."""
        return self.menu.refuse(ctx, path, event, OWNER_ONLY,
                                lambda view, record: self.card(ctx, view, record.get('tab', CONFIG),
                                                               record.get('chat_id') or ''))

    def click(self, ctx, event, value, owner):
        """A click on a settings card; the outcome its ``card action`` line records."""
        entry = self.menu.entry(ctx, event, value)
        if entry is None:
            return 'main_panel_unknown'
        token, path, _ = entry
        outcome, confirm = 'main_panel', False
        if not owner:
            outcome, notice = 'owner_only', OWNER_ONLY
        elif value['kind'] == SECTION:
            notice = '' if value.get('open') in SECTIONS else GONE  # 展开: no setting changes, the view's ``open``.
        elif value['kind'] == MODE and MODE_CHOICES.get(value.get('option'), (True,))[0] is False \
                and butler_settings.load(ctx.state_dir).enabled:
            notice, confirm = '', True  # 每条直接开一个话题 is saved by 确定改 only (mode_confirm).
        else:
            changes = self.changes(ctx, event, value)
            notice = GONE if changes is None else ''
            if changes:
                try:
                    butler_settings.update(ctx.state_dir, **changes)
                    notice = MODE_NOTICES.get(changes.get('enabled')) or saved_notice(value['kind'])
                except Unreadable as exc:  # Nothing written: the settings as they were, not reset (W1-F).
                    self.topics.runtime.log(f'main thread settings not saved: {exc}'[:300])
                    notice = UNREADABLE_NOTICE
            elif value['kind'] in RETIRED_KINDS:
                notice = RETIRED_NOTICE
            elif value['kind'] in OFFLINE_KINDS:
                notice = OFFLINE_NOTICE
        with self.menu.lock(path):
            record = self.menu.clicked(path, event)  # Read again: another click may have drawn it since.
            if record is None:
                return 'main_panel_unknown'
            view = {**record['view'], 'token': token, 'notice': notice}
            view['revision'] += 1
            view.pop('confirm', None)  # Any other click closes the question 每条直接开一个话题 asked.
            if confirm:
                view['confirm'] = True
            view.pop('more', None)  # 更多 stays open after a click on one of its rows (cards/butler_panel.py MORE_KINDS).
            if outcome == 'main_panel' and value['kind'] in MORE_KINDS:
                view['more'] = True
            # 配置 opens the section 展开 names, or the one of the row clicked (a card drawn before the sections too).
            part = value.get('open') if value['kind'] == SECTION else PART_OF.get(value['kind'])
            if outcome == 'main_panel' and part in SECTIONS:
                view['open'] = part
            tab = record.get('tab', CONFIG)
            if view.get('hub') and outcome == 'main_panel':
                tab = PAGE_OF.get(value['kind'], tab)  # The page of the row it changed (a card from before the pages).
                record = {**record, 'tab': tab}
            if not self.menu.publish(ctx, path, record, view, self.card(ctx, view, tab, record.get('chat_id') or '')) \
                    and notice:
                self.menu.unsaved(ctx, record, notice + '\n管家设置卡未能刷新，请重新打开。', f"{token}-{view['revision']}")
        return outcome

    def changes(self, ctx, event, value):
        """The settings fields the click sets: {} when nothing changes, None when its choice is no longer offered."""
        saved = butler_settings.load(ctx.state_dir)
        settings = butler_harness.drawn(saved, self.topics.apps)
        defaults = settings.topic_defaults
        kind, option = value['kind'], value.get('option')
        if kind == ENABLE:
            return {'enabled': value.get('on')} if isinstance(value.get('on'), bool) else None
        if kind == MODE:  # 交给管家 (每条直接开一个话题 waits for 确定改: click); the mode it is already: nothing.
            on = MODE_CHOICES.get(option, (None,))[0]
            return None if on is None else {} if on == settings.enabled else {'enabled': on}
        if kind in RETIRED_KINDS or kind in OFFLINE_KINDS:  # 直接开工, 记住的工作区 and the 工作区 rows are gone: an older card's click changes nothing.
            return {}
        if not isinstance(option, str):
            return None
        if kind == ASSIST:  # 辅助模型 (app/assist.py): bot-wide, whether the butler is on or not.
            from covey.app import assist
            picked = assist.chosen(option, assist.offered(self.topics, state=ctx.state_dir))
            if picked is None:
                return None
            picked = assist_pick(saved.assist, picked, available_models(self.topics, ctx) if picked.harness else {})
            return {} if picked == saved.assist else {'assist': picked}
        choices = available_models(self.topics, ctx)
        page, change = ROW_CHANGES.get(kind, (None, None))
        if change is None:
            return None
        if page == REVIEW:
            # 独立复核's: as the butler's rows, a model named once a Harness is chosen.
            review = settings.review
            rows = Rows(review.harness, review.model, review.effort, False,
                        lambda: (entry_of(choices, review.harness, review.model) if review.model
                                 else default_entry(choices, review.harness)),
                        lambda entry, option: budget_effort(entry, option) if option else '',
                        lambda **fields: {'review': dataclasses.replace(review, **fields)})
        elif page == ASSIST_SCOPE:
            # 辅助模型's: as 专项负责人's, a model named once a Harness is chosen; none under 自动 or 不用模型.
            chosen = saved.assist
            if not chosen.harness or chosen.off:
                return None
            rows = Rows(chosen.harness, chosen.model, chosen.effort, False,
                        lambda: (entry_of(choices, chosen.harness, chosen.model) if chosen.model
                                 else default_entry(choices, chosen.harness)),
                        lambda entry, option: budget_effort(entry, option) if option else '',
                        lambda **fields: {'assist': dataclasses.replace(chosen, **fields)})
        elif page == LEAD:
            if not projects.enabled():
                return None  # A card drawn while projects were on: the section is gone.
            # 专项负责人's: as 独立复核's, a model named once a Harness is chosen.
            lead = settings.lead
            rows = Rows(lead.harness, lead.model, lead.effort, False,
                        lambda: (entry_of(choices, lead.harness, lead.model) if lead.model
                                 else default_entry(choices, lead.harness)),
                        lambda entry, option: budget_effort(entry, option) if option else '',
                        lambda **fields: {'lead': dataclasses.replace(lead, **fields)})
        elif page == BUTLER:
            # The butler's own Harness; no model is its default model (default_entry), whose budgets the 思考预算 row
            # offers and the turn runs with. Its topic.json takes a budget as it is (app/butler.py keep_topic): 推荐 is
            # saved as the level it names now.
            harness = settings.butler_harness
            # A model or budget picked while the settings name no Harness is that Harness's (the one it runs on now,
            # ``drawn``): it is saved with it, so Claude Code enabled later does not take a model id it does not know.
            pinned = {} if saved.harness or harness == CLAUDE else {'harness': harness}  # Claude Code is saved as ''.
            rows = Rows(harness, settings.model, settings.effort, False,
                        lambda: (entry_of(choices, harness, settings.model) if settings.model
                                 else default_entry(choices, harness)),
                        lambda entry, option: budget_effort(entry, option) if option else '',
                        lambda **fields: {**pinned, **fields} if fields else fields)
        else:
            rows = Rows(defaults.harness, defaults.model, defaults.effort, True,
                        lambda: (entry_of(choices, defaults.harness, defaults.model) if defaults.model
                                 else self.fallback(ctx, defaults, choices)),
                        lambda entry, option: option,
                        lambda **fields: {'topic_defaults': dataclasses.replace(defaults, **fields)})
        return change(rows, choices, option, ctx.state_dir)

    @staticmethod
    def notes(settings, value, fields):
        """A workspace's 用途 line from its input (note_<i>), empty removes it: what the web page's old 工作区 page's 用途 saves
        (app/web_settings.py change_note); the card has no 保存用途 button any more."""
        named = value.get('workspaces')
        if not isinstance(named, list) or not isinstance(fields, dict):
            return None
        notes = dict(settings.workspace_notes)
        for i, name in enumerate(named):
            text = fields.get(f'note_{i}')
            if not isinstance(name, str) or not isinstance(text, str):
                continue
            text = ' '.join(text.split())[:NOTE_LENGTH]
            if text:
                notes[name] = text
            else:
                notes.pop(name, None)
        return {'workspace_notes': tuple(notes.items())}


def settings_panel(env, request):
    """The tool ``settings_panel``: the 设置 card on its 管家 tab in the owner's private chat (env.settings,
    app/settings_hub.py), quoting the owner's message of the butler job that asked (at the top level); the
    设置 card it sent before is recalled."""
    chat = env.chat()
    if not chat:
        return reply(False, '主人还没在私聊里给机器人发过消息，设置卡发不出去。')
    if env.settings is None:
        return reply(False, '设置卡不可用。')
    job = read_job_record(job_file(env.state_dir, request.job_key)) or {}
    quote = str(job.get('message_id') or '') if job.get('chat_id') == chat else ''
    ctx = SimpleNamespace(state_dir=Path(env.state_dir), profile=env.profile)
    source = 'tool:' + request.id
    env.settings.open_main(ctx, chat, source, quote=quote)
    if not env.settings.sent(ctx, chat, source):
        return reply(False, '设置卡没有发出去，可以稍后再试一次。')
    return reply(True, '设置卡已发到私聊，主人在卡上改；你不能改设置。')


TOOLS = {"settings_panel": settings_panel}
