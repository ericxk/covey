"""Launch-card fixtures shared by test_launch_models, test_harness_switch and others: a MASO model catalog as
the Router serves it."""
from __future__ import annotations

import json
from types import SimpleNamespace

from covey.app.launch_models import refresh_catalog


def maso_provider(model, effort='', client='AzureResponsesAPI'):
    """An official MASO preset as the Router serves it: a model's presets differ only in their budget.
    Seed's (OpenAI) plain preset turns thinking off."""
    if client == 'OpenAI':
        args = {'model': model, 'thinking': {'type': 'enabled' if effort else 'disabled'},
                **({'reasoning_effort': effort} if effort else {})}
    else:
        args = {'model': model, **({'reasoning_effort': effort, 'reasoning_summary': 'auto'} if effort else {})}
    return {'client_type': client, 'client_args': {'base_url': 'https://router.invalid'}, 'request_args': args}


# A MASO catalog in its upstream order: each preset named by its provider id, a model's presets interleaved.
MASO_PROVIDERS = {
    'seed-stable': maso_provider('seed', client='OpenAI'), 'seed-stable-reasoning': maso_provider('seed', 'medium', 'OpenAI'),
    'es1_orange_o50': maso_provider('orange'), 'gpt56_sol': maso_provider('gpt-5.6-sol'),
    'gpt55_reasoning_high': maso_provider('gpt-5.5', 'high'), 'gpt56_sol_reasoning_high': maso_provider('gpt-5.6-sol', 'high'),
    'gpt55_reasoning_xhigh': maso_provider('gpt-5.5', 'xhigh'), 'gpt56_sol_reasoning_xhigh': maso_provider('gpt-5.6-sol', 'xhigh'),
    'gpt-6-astra': maso_provider('gpt-6-astra'), 'gpt55': maso_provider('gpt-5.5'),
    'gpt56_sol_reasoning_max': maso_provider('gpt-5.6-sol', 'max'),
    'seed-stable-reasoning-high': maso_provider('seed', 'high', 'OpenAI'),
    # Budget presets only, no plain one.
    'es1-grape-g61s_reasoning_max': maso_provider('grape', 'max'), 'es1-grape-g61s_reasoning_xhigh': maso_provider('grape', 'xhigh'),
    # No other preset shares its configuration, so MASO gives it no budget of its own.
    'gpt-6-astra_reasoning_max': maso_provider('gpt-6-astra', 'max', 'Anthropic'),
}
MASO_MENU = {'maso:seed-stable': 'MASO · Seed Stable',
             'maso:gpt56_sol': 'MASO · GPT-5.6 Sol',
             'maso:gpt55': 'MASO · GPT-5.5',
             'maso:gpt-6-astra': 'MASO · GPT-6 Astra',
             'maso:gpt-6-astra_reasoning_max': 'MASO · GPT-6 Astra · max',
             'maso:es1-grape-g61s_reasoning_xhigh': 'MASO · ES1 Grape G61s'}


def write_maso_catalog(state_dir, *, families=True):
    """MASO_PROVIDERS through MASO's own catalog and refresh_catalog, as the bridge writes launch-models/maso.json.
    Without ``families``: the file as releases before one entry per model wrote it. Returns MASO's catalog."""
    from covey_internal.maso.models import build_catalog  # The company extension.

    catalog = build_catalog(MASO_PROVIDERS)
    backend = SimpleNamespace(refresh_model_catalog=lambda ctx: catalog)
    refresh_catalog(SimpleNamespace(backend=backend, spec=SimpleNamespace(name='maso')), SimpleNamespace(state_dir=state_dir))
    if not families:
        path = state_dir / 'launch-models' / 'maso.json'
        data = json.loads(path.read_text())
        for row in data['models'].values():
            row.pop('family', None)
        path.write_text(json.dumps(data))
    return catalog
