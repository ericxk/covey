"""The launch card's choices: a model change (a Harness or provider choice is one, to its first model)
rebuilds the card for that model; a budget, work mode, 敏感词脱敏, workspace or run mode is recorded and
redraws the card too, as its summary names them (cards/launch_config.py, 2026-11-03). A settings row's options
open under it in the client (2026-11-04); a card sent with options pages (84caa8a) sends a row's tap, which redraws
the card with that row open, and its ‹ 返回, which redraws it with none: there a choice, the current one too, closes
the row. Cards sent before have selects, whose callbacks are the same kinds."""
from __future__ import annotations

import re

from covey.app.card_queue import CardActionQueue
from covey.app.form_cards import LAUNCH_FORM_LOCK
from covey.app.launch import (form_draft, form_topic_root, rebuild_launch_card, refuse_retired_form,
                                       retired_form, retired_form_at)
from covey.cards.launch_config import (BUDGET_ACTION, HARNESS_ACTION, MANAGE_OPTION, MODE_ACTION, MODEL_ACTION,
                                                PAGE_ACTION, PAGES, PROVIDER_ACTION, REDACTION_ACTION, REDACTION_LABELS,
                                                WORK_MODE_ACTION, WORKSPACE_ACTION)
from covey.core.menu_choice import find_choice, parse_budget, tier_pick
from covey.core.locks import resource_lock
from covey.core.state import read_json, write_json
from covey.core.topics import load_topic
from covey.app.card_actions import action_value
from covey.app.launch_models import available_models
from covey.core.workspaces import RUN_MODES


class LaunchModelPicker(CardActionQueue):
    """Reserve model intent synchronously, then publish the new model's budgets.

    A one-card form is replaced by a new card (new token) for the chosen model;
    the old token can no longer start. Revision-bearing effort values reject a
    Start click with a previous model's budget, including uncertain API writes.
    """
    def __init__(self, topics):
        super().__init__(topics, name='launch-model', workers=2, label='launch model update')

    action = staticmethod(action_value)

    def matches(self, event):
        return self.action(event).get('kind') in (HARNESS_ACTION, PROVIDER_ACTION, MODEL_ACTION, BUDGET_ACTION,
                                                  WORK_MODE_ACTION, REDACTION_ACTION, WORKSPACE_ACTION, MODE_ACTION,
                                                  PAGE_ACTION)

    def claim(self, ctx, event, raw, owner):
        """The click's outcome when it is a launch card's choice, else None (app/unified.py EARLY)."""
        if not self.matches(event):
            return None
        self.dispatch(ctx, event)
        return 'launch_option'

    @staticmethod
    def save(path, record):
        write_json(path, record, ensure_ascii=False)

    def dispatch(self, ctx, event):
        if not self.topics.runtime.access.allows(event.get('operator_id', ''), event.get('chat_id', '')):
            return
        token, kind = self.action(event).get('token', ''), self.action(event).get('kind')
        # An options-page card's row tap carries the row, a choice its option (a select's callback or an option line's value).
        model = self.action(event).get('page') if kind == PAGE_ACTION else event.get('option')
        if not isinstance(token, str) or not re.fullmatch('[0-9a-f]{24}', token) or not isinstance(model, str):
            return
        path = ctx.state_dir / 'launch-forms' / (token + '.json')
        if (retired := retired_form_at(path, event)) is not None:
            self.submit('retired form notice dropped', refuse_retired_form, self.topics, ctx, retired, event)
            return
        with resource_lock(LAUNCH_FORM_LOCK, path):
            try:
                record = read_json(path)
                topic = load_topic(ctx.state_dir, record['topic_id'])
            except (OSError, ValueError, KeyError):
                return
            if (retired_form(record) or record.get('status') != 'ready' or record.get('picker') or not record.get('card_id')
                    or record.get('message_id') != event.get('message_id')
                    or record.get('chat_id') != event.get('chat_id')
                    or topic.get('chat_id') != event.get('chat_id')
                    or record.get('harness_id') != topic.get('harness_id')
                    or record.get('binding_revision') != topic.get('binding_revision', 1)
                    or topic.get('workspace_locked') or topic.get('harness_locked')
                    or any((ctx.state_dir / 'topics' / record['topic_id']).glob('*_session_id'))):
                return
            page = record.get('page') or ''
            if kind == PAGE_ACTION:
                if not record.get('compact') or model == page or model not in PAGES:
                    return
                record['page'] = model
                revision = record.get('config_revision', 0) + 1
                record.update(config_pending=True, config_revision=revision)
                self.save(path, record)
                self.submit('configuration remains pending', self.process, ctx, path, revision)
                return
            record['page'] = ''  # Any choice returns to the list.
            if kind in (WORKSPACE_ACTION, MODE_ACTION, WORK_MODE_ACTION, REDACTION_ACTION, BUDGET_ACTION):
                # On a page, the option already chosen (or one no longer offered) returns to the list too.
                if not record.get('compact') or not (self.record_choice(record, kind, model) or page):
                    return
                # Recorded, then the card is redrawn with it (process): its summary names the choice. Only the
                # latest revision redraws; a budget from a card a redraw is replacing no longer matches it.
                revision = record.get('config_revision', 0) + 1
                record.update(config_pending=True, config_revision=revision)
                self.save(path, record)
                self.submit('configuration remains pending', self.process, ctx, path, revision)
                return
            if kind in (HARNESS_ACTION, PROVIDER_ACTION):
                # Its first model (the chat's latest first) as if chosen in the model select, which then lists
                # that Harness's (or provider's) models only.
                model = tier_pick(record['models'], record.get('selected_model', ''),
                                   **({'harness': model} if kind == HARNESS_ACTION else {'provider': model}))
                if model is None:
                    if not page:
                        return
                    model = record.get('selected_model', '')  # Its own Harness or provider: back to the list.
            if model not in record['models']:
                return
            live = available_models(self.topics, ctx)
            # A budget preset on a card sent before one entry per model opens its model with that preset's budget.
            model, budget = find_choice(live, model) or (model, '')
            choice = live.get(model)
            same = record.get('selected_model') == model
            if (choice and same and not budget and not record.get('config_pending') and not page
                    and choice == record['models'].get(model)):
                return
            revision = record.get('config_revision', 0) + 1
            # The same model, from its page, keeps its budget; another starts from its own.
            record.update(config_pending=True, config_revision=revision, selected_model=model,
                          selected_effort=budget or (record.get('selected_effort', '') if same else ''))
            if not choice:
                self.save(path, record)
                self.topics.messenger.send_message(ctx.profile, record['chat_id'],
                    '该模型已不在当前可用目录中，尚未启动任务；请选择其他模型。',
                    self.topics.voice.key('launch-model-removed', token + '-' + str(revision)),
                    reply_to_message_id=topic['root_id'], voice=self.topics.voice)
                return
            record['models'][model] = choice
            self.save(path, record)
            # A full queue must fail closed; an older budget may never start.
            self.submit('configuration remains pending', self.process, ctx, path, revision)

    @staticmethod
    def record_choice(record, kind, option):
        """Record a workspace, run mode, work mode, 敏感词脱敏 or budget chosen on the card; whether it was one
        of the select's options and changed the record. A workspace removed since the card was sent is kept,
        so 开始任务 reports it instead of starting in the previous choice."""
        choice = record['models'][record['selected_model']]
        if kind in (WORKSPACE_ACTION, MODE_ACTION):
            if not (option in RUN_MODES if kind == MODE_ACTION else 0 < len(option) <= 200 and option != MANAGE_OPTION):
                return False
            field, place = ('workspace' if kind == WORKSPACE_ACTION else 'run_mode'), record.setdefault('form_defaults', {})
            changed, place[field] = place.get(field) != option, option
            return changed
        if kind == WORK_MODE_ACTION:
            field, valid = 'selected_work_mode', option in (choice.get('work_modes') or ())
        elif kind == REDACTION_ACTION:
            # Shown only for a model that can redact, and checked again at 开始任务.
            field, valid = 'selected_redaction', option in REDACTION_LABELS and bool(choice.get('redaction'))
        else:
            option = parse_budget(option, record['config_revision'], choice)
            field, valid = 'selected_effort', option is not None
        if not valid or record.get(field) == option:
            return False
        record[field] = option
        return True

    def process(self, ctx, path, revision):
        with resource_lock(LAUNCH_FORM_LOCK, path):
            record = read_json(path)
            if (record.get('status') != 'ready' or record.get('picker')
                    or record.get('config_revision') != revision):
                return
            if not self.replace_card(ctx, path, record):
                self.topics.messenger.send_message(ctx.profile, record['chat_id'],
                    '配置没有更新，尚未启动任务；请重新选择后再开始。',
                    self.topics.voice.key('launch-model-failed', path.stem + '-' + str(revision)),
                    reply_to_message_id=form_topic_root(ctx, record), voice=self.topics.voice)

    def replace_card(self, ctx, path, record):
        """Rebuild the whole launch card for the new model, like a directory-picker return.

        Replacing only the budget element inside the form left the Lark client
        unable to submit that form: 开始任务 sent no callback and showed no error.
        """
        # Unsent inputs never reach the bridge; keep what the card was built with, unfolded where it was chosen,
        # and the row an options-page card's tap opened.
        draft = {**form_draft(record), 'expanded': True, 'page': record.get('page') or ''}
        return rebuild_launch_card(self.topics, ctx, path, record, draft, kind='launch-model')
