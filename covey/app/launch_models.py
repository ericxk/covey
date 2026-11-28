"""Local launch choices; slow native catalog probes never run on card clicks."""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import NamedTuple

from covey.core.harness import backend_capabilities
from covey.core.loops import every
from covey.core.model_names import readable_model
from covey.core import gateways
from covey.core.providers import RETIRED_PREFIXES, menu_name, own_account, provider_of
from covey.core.roster import NAMES
from covey.core.state import read_json, read_object, write_json

def spelled(text):
    return re.sub('[-_ ]+', ' ', text.lower())


def readable_name(model, name):
    """A name that only spells out its id (gpt 6 astra, ali-deepseek-v4.1-flash) reads like the Codex and
    Claude entries (GPT-6 Astra, DeepSeek V4.1 Flash); a name of its own (DeepSeek V4 Pro) is kept."""
    return readable_model(model) if spelled(name) == spelled(model) else name


def readable_names(models):
    return {model: {**row, 'display_name': readable_name(model, str(row.get('display_name') or model))}
            for model, row in models.items()}


def codex_names(models):
    """Codex's models read like the others: its gateways' ones ("gpt-6-astra (Acme)") and its own ones that only spell
    out their id ("GPT-6-Luna", which reads oddly next to the same model from another gateway) as "GPT-6 Astra", "GPT-6 Luna"."""
    rows = {}
    for model, row in models.items():
        gateway = gateways.for_model('codex', model)
        bare = model.removeprefix(gateway.prefix) if gateway else model
        name = str(row.get('display_name') or bare)
        if gateway:
            name = name.removesuffix(f' ({gateway.display})')
        rows[model] = {**row, 'display_name': readable_name(bare, name)}
    return rows


def claude_names(models):
    """Claude Code names a gateway's models "Claude Opus 5"; they read "Opus 5" like its own entries
    (Claude · Opus 5 next to Claude · Opus 5.5, not Claude · Claude Opus 5)."""
    return {model: {**row, 'display_name': str(row.get('display_name') or model).removeprefix('Claude ')}
            for model, row in models.items()}


def catalog_changed(choice, live):
    """Whether a card's menu entry is gone from the live catalogue or offers other budgets there."""
    return not live or any(live.get(k) != choice.get(k) for k in ('levels', 'recommended', 'presets'))


def run_choice(choice, effort):
    """(model, effort) a menu entry starts with for a budget ('' the native default): a budget-preset model the
    preset of that budget, with that budget, so the runtime's exact preset check holds."""
    presets = choice.get('presets')
    if presets is None:
        return choice['model'], effort
    if effort not in presets:
        raise ValueError('该模型不支持此思考预算，可选：' + ' / '.join(choice['levels']))
    return presets[effort], (choice.get('run_efforts') or {}).get(effort, effort)


# The model selects' Harness blocks, the owner's most used first; a Harness not
# listed follows in roster order (an extension's Harness where its manifest puts it). The models a chat chose lately come before these blocks (app/model_usage.py).
MENU_ORDER = ('claude',)


def harness_rank(name):
    return MENU_ORDER.index(name) if name in MENU_ORDER else len(MENU_ORDER) + (NAMES.index(name) if name in NAMES else len(NAMES))


def first_models(models, first):
    """``models`` with those ``first`` names (harnesses.json's ``models_first`` for the Harness) first, in that
    order, the rest as they were. A name is an entry's model id or one of its presets."""
    if not first:
        return models
    def rank(item):
        model, row = item
        names = {model, *(row.get('presets') or {}).values()}
        return next((i for i, name in enumerate(first) if name in names), len(first))
    return dict(sorted(models.items(), key=rank))


class CatalogFilter(NamedTuple):
    """How a Harness's cached catalog becomes its part of the menu (available_models)."""
    hidden: tuple = ()  # Model id prefixes never offered, matched on the id after any "<gateway>/" in front.
    live: object = None  # (models, app) → the models still served, or None.
    view: object = dict  # The catalog as the model selects show it, where its own names or order are not readable.

    def apply(self, models, app):
        # Nor a removed provider's model from a catalog saved before it was removed.
        models = {model: row for model, row in models.items()
                  if not model.startswith(tuple(RETIRED_PREFIXES)) and not model.rsplit('/', 1)[-1].startswith(self.hidden)}
        return self.view(self.live(models, app) if self.live else models)


# Harness name → its catalog filter; a Harness not listed offers its catalog as it is, but for RETIRED_PREFIXES, or
# as its backend says (``catalog_filter``). Claude offers Opus 5 and up, and Sonnet 5.5
# and Haiku 5.5 by their aliases sonnet and haiku (nothing below Opus 5): no
# claude-sonnet-*, claude-haiku-* or 4.x id, the older versions.
CATALOG_FILTERS = {
    'claude': CatalogFilter(hidden=('claude-sonnet-', 'claude-haiku-', 'claude-opus-4-'),
                            view=claude_names),
    'dsh': CatalogFilter(view=readable_names),
    'codex': CatalogFilter(view=codex_names),
}


def catalog_filter(app):
    """How ``app``'s catalog becomes its part of the menu: CATALOG_FILTERS' entry for its Harness, else what its
    backend declares (``catalog_hidden``, ``catalog_live(models, state_dir)``, ``catalog_view(models)``; a backend
    must not import this module), else the catalog as it is."""
    known = CATALOG_FILTERS.get(app.spec.name)
    if known:
        return known
    backend = app.backend
    live = getattr(backend, 'catalog_live', None)
    state_dir = getattr(getattr(app.harness, 'context', None), 'state_dir', None)
    return CatalogFilter(hidden=tuple(getattr(backend, 'catalog_hidden', ())),
                         live=(lambda models, _app: live(models, state_dir)) if callable(live) else None,
                         view=getattr(backend, 'catalog_view', dict))


FRESH = 900  # Seconds a catalog its Harness last answered with is offered.
# A refresh that got no answer at all (RuntimeError: the Claude probe or a backend's helper timed out on a
# loaded machine, and the launch and 换配置 cards would lose both Harnesses for minutes) keeps the
# last answer offered this long after it; one answered without models (ValueError) does not.
KEEP_UNANSWERED = 3600


def refresh_catalog(app, ctx):
    refresh = getattr(app.backend, 'refresh_model_catalog', None)
    catalog = refresh(ctx) if callable(refresh) else app.models.backend_model_catalog(ctx)
    if not isinstance(catalog, dict) or not catalog:
        raise ValueError('模型目录暂不可用')
    clean = {}
    for model, row in catalog.items():
        if row.get('status') in ('deprecated', 'removed') or row.get('hidden'):
            continue
        # A level served by another provider (a budget preset) is that model, not
        # a budget of this one; offering it would pair the model with a foreign preset.
        levels = [r['effort'] for r in row.get('supported_reasoning_levels', [])
                  if isinstance(r.get('effort'), str) and r['effort'] and r.get('provider', model) == model]
        # Every budget a catalog maps a preset to, each the preset that serves it: a backend's catalog_view lists the model once.
        family = {r['effort']: r['provider'] for r in row.get('supported_reasoning_levels', [])
                  if isinstance(r.get('effort'), str) and r['effort'] and isinstance(r.get('provider'), str)}
        recommend = getattr(app.backend, 'recommended_reasoning_effort', None)
        recommendation = recommend(ctx, model, levels) if callable(recommend) else ''
        clean[model] = {'display_name': row.get('display_name') or model,
                        'levels': levels, 'default': row.get('default_reasoning_level') or '', 'recommended': recommendation,
                        **({'family': family} if family else {})}
    write_json(ctx.state_dir / 'launch-models' / (app.spec.name + '.json'), {'updated_at': time.time(), 'models': clean},
               ensure_ascii=False)
    return clean


def available_models(topics, ctx):
    """Launchable models by route key in a fixed order: a block per Harness in MENU_ORDER, each in its
    catalog's order but the Harness's ``models_first`` (harnesses.json) ahead. The launch and 换配置 cards list
    them by the chat's recent use first (app/model_usage.ranked); models nobody chose lately keep this order."""
    result = {}
    for name in sorted(topics.apps, key=harness_rank):
        app = topics.apps[name]
        # A Harness with work modes offers them with each model, its default first.
        modes = {'work_modes': list(app.backend.work_modes)} if getattr(app.backend, 'work_modes', ()) else {}
        capabilities = backend_capabilities(app.backend, ctx)
        # A Harness that can run the butler (its Bridge tools), on the 设置 card's 管家 tab (cards/butler_panel.py).
        modes = {**modes, 'bridge_tools': True} if capabilities.bridge_tools else modes
        # One the Bridge compacts: the 管家 tab's 压缩阈值 counts only there.
        modes = {**modes, 'compaction': True} if capabilities.compaction else modes
        try:
            cached = read_json(ctx.state_dir / 'launch-models' / (name + '.json'))
            # A failed background probe must not keep removed models forever; one that got no answer keeps the
            # last answer an hour (KEEP_UNANSWERED).
            keep = KEEP_UNANSWERED if cached.get('unanswered_at', 0) > cached['updated_at'] else FRESH
            if not 0 <= time.time() - cached['updated_at'] <= keep:
                continue
            models = catalog_filter(app).apply(cached['models'], app)
            models = first_models(models, (getattr(app.harness.context, 'options', None) or {}).get('models_first'))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue  # Only freshly verified native catalogs are launchable.
        for model, row in models.items():
            # The route is part of the model choice, never inferred from a name.
            provider = provider_of(name, model)
            source = {'source': provider.name} if provider and provider.name else {}
            # The 换配置 card's Harness and provider selects group the entries by these (cards/switch_form.py).
            route = {'title': app.spec.title, 'provider': provider.key if provider else '',
                     'provider_title': menu_name(provider, app.spec.title)}
            result[name + ':' + model] = {**row, **source, 'harness': name, 'model': model,
                                          'label': model_label(app.spec.title, {**row, **source}), **route, **modes}
    return fold_same_account(result)


def fold_same_account(result):
    """``result`` with each Harness's own login shown once when another provider of it is the same account
    (core/providers.py ``own_account``, T786): the own login's entries a folded gateway offers too (by name) leave the menu, the others are listed
    under the gateway, so the provider select has one entry for the account. Their ids are unchanged: they still run on the
    Harness's own login, which is that account; a topic on one runs as before."""
    for name in {entry['harness'] for entry in result.values()}:
        names = own_account(name)
        if not names:
            continue
        theirs = [entry for entry in result.values() if entry['harness'] == name and entry.get('provider') in names]
        if not theirs:
            continue
        offered = {gateway_free(str(entry.get('display_name') or '')) for entry in theirs}
        first = theirs[0]
        for key, entry in list(result.items()):
            provider = provider_of(name, entry['model'])
            if entry['harness'] != name or provider is None or provider.prefix:
                continue  # Another Harness's, or a gateway's.
            if gateway_free(str(entry.get('display_name') or '')) in offered:
                del result[key]
                continue
            moved = {**entry, 'provider': first['provider'], 'provider_title': first['provider_title'],
                     'source': first.get('source') or first['provider_title']}
            result[key] = {**moved, 'label': model_label(str(entry.get('title') or name), moved)}
    return result


def model_label(title, row):
    """A model as every model select, receipt and task card names it: Harness · provider · model, the Harness's
    own provider left out (Codex · Acme · GPT-6 Astra, Claude · Opus 5.5; core/providers.py). What tells two apart
    comes before the model's name, as a topic's side panel shows only about 20 characters of an
    option."""
    return ' · '.join([title, *([str(row['source'])] if row.get('source') else []), str(row['display_name'])])


def gateway_free(name):
    """``name`` without a gateway's " (display name)" suffix."""
    return next((name.removesuffix(f' ({g.display})') for g in gateways.all_gateways() if name.endswith(f' ({g.display})')),
                name)


def shown_model(state_dir, harness, model):
    """How the answer card's 输入 names the model ``model`` (an id a backend reported it ran, such as Claude's
    claude-opus-5-5[1m]) of the Harness ``harness``: its cached catalog's name (the launch card's), else Claude's
    family and version (Opus 5.5), else the id; "" without one. Reads the cache only, never asks the backend."""
    model = str(model or "").strip()
    if not model:
        return ""
    bare = re.sub(r"\[[^\]]*\]$", "", model)
    rows = read_object(Path(state_dir) / 'launch-models' / (str(harness) + '.json')).get('models')
    rows = rows if isinstance(rows, dict) else {}
    for key in (model, bare):
        row = rows.get(key)
        if isinstance(row, dict) and row.get('display_name'):
            return gateway_free(str(row['display_name']).removeprefix('Claude '))
    family = re.fullmatch(r"claude-([a-z]+)-(\d+)(?:-(\d{1,2}))?(?:-\d{8})?", bare)
    if family:
        return f"{family[1].capitalize()} {family[2]}" + (f".{family[3]}" if family[3] else "")
    return bare.rsplit('/', 1)[-1]


def note_unanswered(ctx, name):
    """Mark ``name``'s cached catalog as not renewed for want of an answer: offered on (KEEP_UNANSWERED)."""
    path = ctx.state_dir / 'launch-models' / (name + '.json')
    cached = read_object(path)
    if 'updated_at' in cached:
        write_json(path, {**cached, 'unanswered_at': time.time()}, ensure_ascii=False)


def refresh_once(topics, app, ctx):
    """One background refresh of ``app``'s catalog; a failure is logged with its reason, none while the Bridge
    stops (its helper dies with it: <Backend>Catalog_helper_failed). An unexpected one is
    logged too: raised, it would end the refresh and the Harness would leave the launch card until a restart."""
    try:
        refresh_catalog(app, ctx)
    except Exception as exc:
        if topics.runtime.shutdown.is_set():
            return
        if isinstance(exc, RuntimeError):
            note_unanswered(ctx, app.spec.name)
        topics.runtime.log(f'launch catalog unavailable harness={app.spec.name} error={type(exc).__name__} '
                           f'detail={" ".join(str(exc).split())[:80]}')


def start_catalog_refresh(topics):
    def refresh(app, ctx):
        every(topics.runtime, 300, refresh_once, args=(topics, app, ctx))
    for name, app in topics.apps.items():
        threading.Thread(target=refresh, args=(app, app.harness.context), daemon=True).start()
