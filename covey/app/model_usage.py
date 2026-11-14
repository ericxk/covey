"""What each chat chose on the model selects: the order they list models in, and each model's usual budget.

<state>/model-usage.json keeps the selections of the last WINDOW_DAYS days: each 开始任务 on a launch card
and each 确认更换 on a 换配置 card, with its menu entry (route key), its budget as the card names it
(top_minus_one for 推荐, a level, default) and its work mode. A chat reads its own selections, a chat
without any reads every chat's, so the acceptance group's test launches never reorder the owner's private
chat. The owner asked for the last used model first and each model's usual budget (2026-11-02): a card
lists its preselected model first (the launch card: the chat's last launch; 换配置: the topic's model), then
the ones the chat chose most. This replaces the fixed order of 2026-10-26, which still orders the models
nobody chose lately.
"""
from __future__ import annotations

import collections
import threading
import time
from pathlib import Path

from covey.core.menu_choice import find_choice
from covey.core.defaults import stored_work_mode
from covey.core.locks import resource_lock
from covey.core.state import read_json, write_json

USAGE = 'model-usage.json'
WINDOW_DAYS = 14
# A model's usual budget: the one chosen most often in its last EFFORT_MEMORY selections, a tie going to the latest.
EFFORT_MEMORY = 10
KEEP = 1000  # The most selections the file keeps.


def usage_path(state_dir):
    return Path(state_dir) / USAGE


def _read(state_dir):
    try:
        events = read_json(usage_path(state_dir))['events']
    except (OSError, ValueError, KeyError, TypeError):
        return []
    if not isinstance(events, list):
        return []
    return [e for e in events if isinstance(e, dict)
            and isinstance(e.get('at'), (int, float)) and isinstance(e.get('model'), str) and isinstance(e.get('effort', ''), str)]


def recent(state_dir, chat_id, now=None):
    """The chat's selections of the last WINDOW_DAYS days, oldest first; every chat's when it has none."""
    since = (time.time() if now is None else now) - WINDOW_DAYS * 86400
    events = sorted((e for e in _read(state_dir) if e['at'] >= since), key=lambda e: e['at'])
    return [e for e in events if e.get('chat') == chat_id] or events


def record(state_dir, chat_id, model, effort, work_mode, kind, form='', now=None):
    """Add one selection: ``model`` its menu entry, ``effort`` the budget option the card had chosen, ``form``
    the launch or switch form's token."""
    now = time.time() if now is None else now
    path = usage_path(state_dir)
    with resource_lock('model-usage', str(path)):
        ensure(state_dir)  # The forms' history first, so the file never starts from this selection alone.
        since = now - WINDOW_DAYS * 86400
        # The history may have read this form already (it is done before it is recorded): counted once.
        events = [e for e in _read(state_dir) if e['at'] >= since and not (form and e.get('form') == form)]
        events.append({'at': now, 'chat': chat_id, 'model': model, 'effort': effort or '', 'work_mode': work_mode or '',
                       'kind': kind, 'form': form})
        write_json(path, {'events': events[-KEEP:]}, ensure_ascii=False)


def ensure(state_dir, now=None):
    """Write model-usage.json from the launch and switch forms of the last WINDOW_DAYS days when there is
    none yet (the release that introduced it): each form that started a topic or switched one, at its last
    change. Returns whether it wrote."""
    path = usage_path(state_dir)
    with resource_lock('model-usage', str(path)):
        if path.exists():
            return False
        since = (time.time() if now is None else now) - WINDOW_DAYS * 86400
        events = []
        for folder, kind in (('launch-forms', 'launch'), ('switch-forms', 'switch')):
            for form in sorted((Path(state_dir) / folder).glob('*.json')):
                try:
                    at = form.stat().st_mtime
                    saved = read_json(form) if at >= since else None
                except (OSError, ValueError):
                    continue
                if isinstance(saved, dict) and saved.get('status') == 'done' and isinstance(saved.get('selected_model'), str):
                    events.append({'at': at, 'chat': str(saved.get('chat_id') or ''), 'model': saved['selected_model'],
                                   'effort': str(saved.get('selected_effort') or ''),
                                   'work_mode': stored_work_mode(str(saved.get('selected_work_mode') or '')), 'kind': kind,
                                   'form': form.stem})
        events.sort(key=lambda e: e['at'])
        write_json(path, {'events': events[-KEEP:]}, ensure_ascii=False)
        return True


def start(state_dir, log):
    """ensure() off the starting thread: the first start of this release reads the forms once."""
    def run():
        try:
            if ensure(state_dir):
                log(f'model usage written from the forms: {len(_read(state_dir))} selections')
        except OSError as exc:
            log(f'model usage not written from the forms: {type(exc).__name__}')
    threading.Thread(target=run, name='model-usage', daemon=True).start()


def ranked(choices, events, first=None):
    """``choices`` (available_models' fixed order) as the model selects list them: ``first`` (the card's
    preselected model) first, then the ones ``events`` selected, most often first (a tie: the more recent),
    then the rest in their fixed order. A budget preset chosen before one entry per model counts for its model."""
    counts, last = collections.Counter(), {}
    for event in events:
        found = find_choice(choices, event['model'])
        if found:
            counts[found[0]] += 1
            last[found[0]] = max(last.get(found[0], 0), event['at'])
    return {key: choices[key] for key in sorted(choices, key=lambda key: (key != first, -counts[key], -last.get(key, 0)))}


def last_selection(choices, events):
    """(menu entry, budget, work mode) of the latest selection still on the menu, None without one: what a
    launch card preselects without a saved launch. The budget is a budget preset's own (find_choice), else ''."""
    for event in reversed(events):
        found = find_choice(choices, event['model'])
        if found:
            return found[0], found[1], str(event.get('work_mode') or '')
    return None


def usual_effort(events, key, options):
    """The budget option chosen most often with menu entry ``key`` in its last EFFORT_MEMORY selections that
    ``options`` (the card's values) still offers, a tie going to the latest; '' without one."""
    chosen = [e.get('effort') for e in events if e['model'] == key and e.get('effort') in options][-EFFORT_MEMORY:]
    if not chosen:
        return ''
    counts = collections.Counter(chosen)
    best = max(counts.values())
    return next(effort for effort in reversed(chosen) if counts[effort] == best)
