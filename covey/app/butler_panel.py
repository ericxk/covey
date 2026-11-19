"""The 管家 settings card (docs/butler.md 设置卡): the owner changes the butler's settings in the private
chat. The butler only sends it (the tool ``settings_panel``, TOOLS); it cannot change its settings.

It is two tabs of the bot menu's 设置 card (app/settings_hub.py), one a page (cards/settings_hub.py BUTLER_PAGES):
私聊默认, 新话题默认; its 记住的工作区 and 工作区用途 buttons are on the 工作区 tab, which app/workspace_manager.py draws
(``pages``: this panel redraws that tab through it after its click). A card on the retired page 其他 (2026-11-08 to
11-09) still acts: its 直接开工 lands on 新话题默认, the rest on 工作区 (PAGE_OF). The tool and, unless COVEY_BUTLER=0, the menu key MENU_KEY send that card on
私聊默认; its first row is the private chat mode (管家, 纯话题); the chat keeps one 设置 card, a new one
recalling the one before (app/bot_menu.py Panels). A click redraws the page of its button (PAGE_OF; the record's
``tab`` follows), so a click on a 设置 card drawn before the pages, all of them on its 管家 tab, lands on the page
of the row it changed. No topic, no job. The 设置 card's record holds the tab's view (revision, notice); a card
sent before there was a 设置 card (gen 286) has its own record, <state>/main-thread-panels/<token>.json, and still
acts. The settings themselves are read from core/butler_settings.py at each draw and written only by
``update``, so every card shows what holds now. Each click changes one field to the state its value names, then
the whole card is drawn again (rule 9). Only the owner acts; anyone else's click is refused on the card.
"""
from __future__ import annotations

import dataclasses
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, NamedTuple

from covey.app.card_actions import form_fields
from covey.app.launch_models import available_models
from covey.core.menu_choice import (budget_effort, default_entry, entry_of, model_provider, models_of, redacts,
                                             tiers)
from covey.app.butler_env import reply
from covey.app.butler_topics import resolve
from covey.app.menu_panel import MenuPanel, hub_top
from covey.cards.butler_panel import (COMPACT, EFFORT, ENABLE, KINDS, MODEL, MODEL_FOLLOW, NOTE_LENGTH,
                                                    NOTES, PAGE_OF, PROVIDER, REDACTION, START, SUBAGENTS,
                                                    TOPIC_EFFORT, TOPIC_HARNESS, TOPIC_MODEL, TOPIC_PROVIDER, TOPIC_REDACTION,
                                                    TOPIC_RUN_MODE, TOPIC_WORKSPACE, UNTRUST, CLAUDE, follow_text,
                                                    butler_panel_card)
from covey.cards.launch_config import effort_choices
from covey.cards.settings_hub import BUTLER, BUTLER_PAGES, OTHER, RETIRED, TOPIC, WORKSPACE
from covey.core import butler_settings
from covey.core.jobs import job_file, read_job_record
from covey.core.butler_settings import SUBAGENTS as SUBAGENT_MODES, TIERS
from covey.core.state import read_object
from covey.core.workspaces import RUN_MODES, config_path

FOLDER = 'main-thread-panels'
LOCK = 'main-thread-panel'
MENU_KEY = 'main_thread_settings'  # The bot menu's event key that opens the 设置 card on this tab.
OWNER_ONLY = '⚠️ 只有机器人主人能改管家的设置。'
GONE = '⚠️ 这个选项已不可用，卡片已按最新设置重画。'
# Said on the card once the private chat mode changed (docs/butler.md 私聊模式).
MODE_NOTICES = {True: '已改成管家：聊天区的消息交给管家。',
                False: '已改成纯话题：私聊顶层的新消息各开一个话题；已开的话题照常在各自的话题里继续。'}
# Said on the card once another setting was saved (docs/ui.md 5.2: 保存后用一句灰字回执), by the page of the row.
SAVED = {BUTLER: '已保存：管家下一轮起照这个来。', TOPIC: '已保存：之后新开的话题照这个来。', WORKSPACE: '已保存。'}


def workspace_names(state_dir):
    """The saved workspaces' names in their order (core/workspaces.py), read only: [] before there is a list."""
    workspaces = read_object(config_path(Path(state_dir))).get('workspaces')
    return [str(name) for name in workspaces] if isinstance(workspaces, dict) else []


def offered(entry, effort, native=True):
    """Whether the 思考预算 row of ``entry`` (a menu entry, or None) offers ``effort`` (cards/butler_panel.py
    effort_choices: '', the launch card's budgets)."""
    return effort in dict(effort_choices(entry, native=native))


def kept(entry, effort, native=True):
    """``effort`` where the newly chosen model ``entry`` offers it too, else '' (its default)."""
    return effort if entry and offered(entry, effort, native) else ''


class Rows(NamedTuple):
    """A page's Harness, 供应商, 模型 and 思考预算 rows as the row handlers (ROW_CHANGES) read and write them: 私聊默认's
    are the butler's own, 新话题默认's a new topic's."""
    harness: str  # '' none chosen: the 新话题默认 rows under it offer nothing.
    model: str
    effort: str
    native: bool  # Whether 模型原生默认 is a budget of its own (effort_choices).
    entry: Callable  # () -> the menu entry whose budgets the 思考预算 row offers (or None).
    budget: Callable  # (entry, option) -> the budget saved for a 思考预算 option.
    save: Callable  # (**fields) -> the settings fields that set them.


def pick_provider(rows, choices, option, state_dir):
    """供应商: a provider with its first model (the model id carries the provider: acme/gpt-6-astra), as on the launch
    card, and the budget where that model offers it too; '' leaves the model to the default."""
    providers = tiers(choices, rows.harness)[1] if rows.harness else {}
    if not rows.harness or option and option not in providers:
        return None
    if option == model_provider(rows.harness, rows.model, choices):
        return {}
    first = next(iter(models_of(choices, rows.harness, option)), '') if option else ''
    if option and not first:
        return None
    return rows.save(model=first, effort=kept(entry_of(choices, rows.harness, first), rows.effort, rows.native))


def pick_model(rows, choices, option, state_dir):
    """模型: any of the Harness's models (a card drawn before may list another provider's), the budget where it offers
    it too."""
    if not rows.harness or option and option not in models_of(choices, rows.harness):
        return None
    return rows.save(model=option, effort=kept(entry_of(choices, rows.harness, option), rows.effort, rows.native))


def pick_effort(rows, choices, option, state_dir):
    entry = rows.entry()
    if not offered(entry, option, native=rows.native):
        return None
    return rows.save(effort=rows.budget(entry, option))


def pick_harness(rows, choices, option, state_dir):
    """新话题默认's Harness: another one empties the model and budget."""
    if option and option not in tiers(choices, option)[0]:
        return None
    return rows.save(**({'harness': option} if option == rows.harness else {'harness': option, 'model': '', 'effort': ''}))


def pick_redaction(rows, choices, option, state_dir):
    """私聊默认's 脱敏: on only where Claude Code can redact."""
    if option not in ('on', 'off') or option == 'on' and not redacts(choices, CLAUDE):
        return None
    return {'redaction': option == 'on'}


# A settings row's kind -> (its page, what a choice on it changes: rows, choices, option, state_dir -> the settings
# fields, {} for none, None when the choice is no longer offered). The two pages share the 供应商, 模型 and 思考预算 rows'.
ROW_CHANGES = {
    PROVIDER: (BUTLER, pick_provider), MODEL: (BUTLER, pick_model), EFFORT: (BUTLER, pick_effort),
    REDACTION: (BUTLER, pick_redaction),
    COMPACT: (BUTLER, lambda rows, choices, option, state_dir: {'compact_tokens': int(option)} if option.isdigit() else None),
    TOPIC_HARNESS: (TOPIC, pick_harness), TOPIC_PROVIDER: (TOPIC, pick_provider), TOPIC_MODEL: (TOPIC, pick_model),
    TOPIC_EFFORT: (TOPIC, pick_effort),
    TOPIC_RUN_MODE: (TOPIC, lambda rows, choices, option, state_dir: (
        rows.save(run_mode=option) if option in RUN_MODES else None)),
    TOPIC_REDACTION: (TOPIC, lambda rows, choices, option, state_dir: (
        rows.save(redaction=option == 'on') if option in ('on', 'off') else None)),
    TOPIC_WORKSPACE: (TOPIC, lambda rows, choices, option, state_dir: (
        rows.save(workspace=option) if not option or option in workspace_names(state_dir) else None)),
    # 子 Agent: bot-wide (backends/claude/subagents.py), not a new topic's default, though on that page.
    SUBAGENTS: (TOPIC, lambda rows, choices, option, state_dir: (
        {'subagents': option} if option in SUBAGENT_MODES else None)),
}


class ButlerPanel:
    def __init__(self, topics):
        """``topics`` (app/topics.py Topics) gives the model catalog's apps and the messenger and voice."""
        self.topics = topics
        # Its buttons act on its pages, the 工作区 tab (记住的工作区, 用途) and the retired 其他.
        self.menu = MenuPanel(topics, 'main-thread-panel', FOLDER, LOCK, tab=(*BUTLER_PAGES, WORKSPACE, OTHER))
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

    def card(self, ctx, view, tab=BUTLER, chat=''):
        """The card of ``view``: on the 设置 card (``hub`` in it) the page ``tab`` (another panel's: ``pages``), else
        the pages one under the other. ``chat``: the chat it is in, whose last launch a new topic follows ('' the
        owner's private chat; a personal group opens its own)."""
        tab = RETIRED.get(tab, tab)
        if view.get('hub') and tab in self.pages:
            return self.pages[tab](ctx, view['token'], {'view': view, 'tab': tab})
        settings, choices = butler_settings.load(ctx.state_dir), available_models(self.topics, ctx)
        page = tab if view.get('hub') and tab in BUTLER_PAGES else None
        topic = page in (None, TOPIC)
        defaults = settings.topic_defaults
        return butler_panel_card(view['token'], view, settings, choices, workspace_names(ctx.state_dir),
                                      top=hub_top(page or BUTLER, view['token'], view), page=page,
                                      follows=self.follows(ctx, defaults, choices, chat) if topic else {},
                                      fallback=self.fallback(ctx, defaults, choices, chat) if topic else None)

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
        """{row key: text} of what each 新话题默认 row's empty option sets (ACCEPT-325 问题 2): ``defaults`` with the
        row and those under it empty (a click on it empties them: changes), resolved now: '跟随上次：opus · max',
        'Bridge 默认：high'. A row whose text cannot be said is left out (默认)."""
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
        return self.card(ctx, record['view'], record.get('tab', BUTLER), record.get('chat_id') or '')

    def tab_shown(self, ctx, token):
        self.topics.runtime.log(f'main thread panel shown token={token[:8]}')

    def refuse(self, ctx, path, event):
        """OWNER_ONLY on the card ``event`` clicked; whether it was a settings card's."""
        return self.menu.refuse(ctx, path, event, OWNER_ONLY,
                                lambda view, record: self.card(ctx, view, record.get('tab', BUTLER),
                                                               record.get('chat_id') or ''))

    def click(self, ctx, event, value, owner):
        """A click on a settings card; the outcome its ``card action`` line records."""
        token = value.get('token')
        if not isinstance(token, str) or not re.fullmatch('[0-9a-f]{24}', token):
            return 'main_panel_unknown'
        path = self.path(ctx, token)
        if self.menu.clicked(path, event) is None:
            return 'main_panel_unknown'
        outcome = 'main_panel'
        if not owner:
            outcome, notice = 'owner_only', OWNER_ONLY
        else:
            changes = self.changes(ctx, event, value)
            notice = GONE if changes is None else ''
            if changes:
                butler_settings.update(ctx.state_dir, **changes)
                notice = MODE_NOTICES.get(changes.get('enabled')) or SAVED.get(PAGE_OF.get(value['kind']), '')
        with self.menu.lock(path):
            record = self.menu.clicked(path, event)  # Read again: another click may have drawn it since.
            if record is None:
                return 'main_panel_unknown'
            view = {**record['view'], 'token': token, 'notice': notice}
            view['revision'] += 1
            tab = record.get('tab', BUTLER)
            if view.get('hub') and outcome == 'main_panel':
                tab = PAGE_OF.get(value['kind'], tab)  # The page of the row it changed (a card from before the pages).
                record = {**record, 'tab': tab}
            if not self.menu.publish(ctx, path, record, view, self.card(ctx, view, tab, record.get('chat_id') or '')) \
                    and notice:
                self.menu.unsaved(ctx, record, notice + '\n管家设置卡未能刷新，请重新打开。', f"{token}-{view['revision']}")
        return outcome

    def changes(self, ctx, event, value):
        """The settings fields the click sets: {} when nothing changes, None when its choice is no longer offered."""
        settings = butler_settings.load(ctx.state_dir)
        defaults = settings.topic_defaults
        kind, option = value['kind'], value.get('option')
        if kind == ENABLE:
            return {'enabled': value.get('on')} if isinstance(value.get('on'), bool) else None
        if kind == START:
            tier, on = value.get('tier'), value.get('on')
            if tier not in TIERS or not isinstance(on, bool):
                return None
            return {'start': tuple(t for t in TIERS if (t == tier and on) or (t != tier and t in settings.start))}
        if kind == UNTRUST:
            return {'trusted': tuple(k for k in settings.trusted if k != value.get('key'))}
        if kind == NOTES:
            return self.notes(settings, value, form_fields(event))
        if not isinstance(option, str):
            return None
        choices = available_models(self.topics, ctx)
        page, change = ROW_CHANGES.get(kind, (None, None))
        if change is None:
            return None
        if page == BUTLER:
            # The butler's own: Claude Code; no model is Claude Code's default model (default_entry), whose
            # budgets the 思考预算 row offers and the turn runs with --effort. Its topic.json takes a budget as it is
            # (app/butler.py keep_topic): 推荐 is saved as the level it names now.
            rows = Rows(CLAUDE, settings.model, settings.effort, False,
                        lambda: (entry_of(choices, CLAUDE, settings.model) if settings.model
                                 else default_entry(choices, CLAUDE)),
                        lambda entry, option: budget_effort(entry, option) if option else '',
                        lambda **fields: fields)
        else:
            rows = Rows(defaults.harness, defaults.model, defaults.effort, True,
                        lambda: (entry_of(choices, defaults.harness, defaults.model) if defaults.model
                                 else self.fallback(ctx, defaults, choices)),
                        lambda entry, option: option,
                        lambda **fields: {'topic_defaults': dataclasses.replace(defaults, **fields)})
        return change(rows, choices, option, ctx.state_dir)

    @staticmethod
    def notes(settings, value, fields):
        """保存用途: each listed workspace's line from its input (note_<i>), empty removes it."""
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
