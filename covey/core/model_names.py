"""How model ids read: a readable name from an id, the budget a preset id ends with, where a budget ranks, and
who makes the model (``vendor_of``).

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


def model_name(model_id):
    """A job's recorded model as every page names it: the route a provider puts before it, the budget a preset ends with, a
    context window in brackets and the brand the Harness already says left out; a version of single numbers joined by
    dots. claude-opus-5-5 → Opus 5.5, router/claude-opus-5-5 → Opus 5.5, opus → Opus, opus[1m] → Opus,
    acme/gpt-6-astra → GPT-6 Astra, gpt56_sol_reasoning_max → GPT-5.6 Sol; '' for none or the backend's own
    default."""
    text = str(model_id or '').strip().rsplit('/', 1)[-1]
    text = re.sub(r'\[[^\]]*\]$', '', text).strip()
    preset = PRESET_RE.fullmatch(text)
    text = preset['base'] if preset else text
    if not text or text.lower() == 'default':
        return ''
    parts = [word for word in re.split('[-_]', text) if word]
    if parts[:1] == ['claude'] and len(parts) > 1:
        parts = parts[1:]
    while len(parts) > 2 and re.fullmatch(r'\d', parts[-1]) and re.fullmatch(r'\d+', parts[-2]):
        parts[-2:] = [parts[-2] + '.' + parts[-1]]
    return readable_model('-'.join(parts))


def effort_rank(effort):
    return -1 if effort is None else EFFORT_ORDER.index(effort) if effort in EFFORT_ORDER else len(EFFORT_ORDER)


# Who makes a model, by the words its id or menu name starts with (``vendor_of``); the first that matches.
VENDORS = (("anthropic", ("claude", "opus", "sonnet", "haiku", "fable")),
           ("openai", ("gpt", "chatgpt", "codex", "o1", "o3", "o4")),
           ("google", ("gemini", "gemma")),
           ("deepseek", ("deepseek",)),
           ("alibaba", ("qwen", "qwq")),
           ("moonshot", ("kimi", "moonshot")),
           ("zhipu", ("glm", "chatglm")),
           ("doubao", ("doubao", "seed")),  # The maker named by its models: the company list keeps its name out of the core.
           ("xai", ("grok",)),
           ("mistral", ("mistral", "codestral", "devstral")))
# A Harness's own maker, for a model whose words name none (its default, "" in a record).
HARNESS_VENDORS = {"claude": "anthropic", "codex": "openai", "pi": "openai", "agy": "google", "dsh": "deepseek"}


def vendor_of(harness, model="", label=""):
    """Who makes the model ``harness`` runs as ``model`` (its id) or ``label`` (its own name, a menu entry's
    display_name: not "Harness · provider · model", whose Harness would name its maker), as a word to compare:
    acme/gpt-6-astra → openai, ali-deepseek-v4.1-flash → deepseek, Opus 5.5 → anthropic; a model that names none (the
    Harness's default, '') is its Harness's maker's (HARNESS_VENDORS), else the Harness's name."""
    words = [word for word in re.split(r'[^a-z0-9]+', f"{model or ''} {label or ''}".lower()) if word]
    for vendor, marks in VENDORS:
        if any(word.startswith(mark) for word in words for mark in marks):
            return vendor
    return HARNESS_VENDORS.get(str(harness or ''), str(harness or ''))
