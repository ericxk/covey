"""How model ids read: a readable name from an id, the budget a preset id ends with, and where a budget ranks.

Standard library and core/defaults only; the menus (app/launch_models.py) and an extension's catalog view use it.
"""

from __future__ import annotations

import re

from covey.core.defaults import EFFORT_ORDER

# A budget preset id ends with its budget (gpt55_reasoning_high, seed-stable-reasoning-high);
# a bare "reasoning" leaves the budget to the catalog's single level.
PRESET_RE = re.compile(r'(?P<base>.+?)[-_]reasoning(?:[-_](?P<effort>' + '|'.join(EFFORT_ORDER) + '))?')


def readable_model(model_id):
    """seed-stable → Seed Stable, gpt55 → GPT-5.5, gpt-6-astra → GPT-6 Astra, es1-grape-g61s → ES1 Grape G61s,
    gpt-5.5-2026-04-24 → GPT-5.5 (2026-04-24), ali-deepseek-v4.1-flash → DeepSeek V4.1 Flash."""
    model_id, date = re.fullmatch(r'(.+?)(?:[-_](\d{4}-\d{2}-\d{2}))?', model_id).groups()
    parts = [word for word in re.split('[-_]', model_id) if word]
    if parts[:1] == ['ali'] and len(parts) > 1:
        parts = parts[1:]  # The host (DeepSeek V4 Pro).
    words = []
    for word in parts:
        version = re.fullmatch(r'gpt(\d)(\d?)', word)
        if version:
            words.append('GPT-' + version[1] + ('.' + version[2] if version[2] else ''))
        elif words and words[-1] == 'GPT' and word[0].isdigit():
            words[-1] += '-' + word
        elif word in BRANDS:
            words.append(BRANDS[word])
        elif word == 'gpt' or re.fullmatch('[a-z]+[0-9]+', word):
            words.append(word.upper())
        else:
            words.append(word[0].upper() + word[1:])
    return ' '.join(words) + (f' ({date})' if date else '') if words else model_id


BRANDS = {'deepseek': 'DeepSeek'}


def effort_rank(effort):
    return -1 if effort is None else EFFORT_ORDER.index(effort) if effort in EFFORT_ORDER else len(EFFORT_ORDER)
