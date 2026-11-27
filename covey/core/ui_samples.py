"""样张 (docs/ui.md 12 改外观先选样张): a checkpoint question's option may carry ``sample``, the absolute path of a JSON file
with a card (schema 2.0) or the part of one being compared, so the owner picks how a card looks from the cards themselves
(the owner, 2026-11-22: 给我不同的款式让我自己选择，这样一次性到位).

``carry`` reads each option's file once, when the question is registered (app/checkpoint_questions.py ``defer``), and keeps
in its place what the question card draws, ``{"elements": [...], "colors": {...}}``, or why it cannot, ``{"note": ...}``:
a card drawn again later draws the same, whatever became of the file. What is kept can do nothing (``inert``): every
``behaviors`` (callbacks and links), a button's callback ``value``, ``confirm`` and a field's ``required`` is taken off, a
``form`` becomes a plain column (its fields drawn, nothing sent), each ``element_id`` and ``name`` is renamed for its sample so two samples never share one, a ``header``
becomes its title as a bold line, and an image whose key is a test's fake one a grey mark. A file over FILE_BYTES, or a
sample over SAMPLE_BYTES or SAMPLE_NODES once inert, is not kept; what the whole card may hold is the card's to count
(cards/question_cards.py ``fitted``).

Standard library and core/card_limits.py only.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from covey.core import card_limits

FIELD = "sample"  # An option's: the path the turn wrote, then what ``carry`` kept.
MOST = 4  # Options of one question that get their sample drawn; the docs ask for 2 or 3.
FILE_BYTES = 60_000  # A sample file larger than this is not read.
SAMPLE_BYTES = 20_000  # One sample's JSON once inert; a card is at most 30 KB (scripts/cardkit_check.py LIMITS).
SAMPLE_NODES = 120  # One sample's tagged nodes once inert; a card is at most 200.
COLORS = 40  # Custom colours (config.style.color) a sample brings at most.
# What a click would do, and what only a form's field may say (CardKit refuses ``required`` outside a form).
DROPPED = frozenset({"behaviors", "confirm", "form_action_type", "multi_url", "url", "required"})
_FAKE_IMAGE = re.compile(r"fake|^img_?$", re.IGNORECASE)
IMAGE_MARK = "<font color='grey'>［图］</font>"

NOT_ABSOLUTE = "样张路径不是绝对路径"
NOT_READ = "样张文件读不到"
TOO_BIG = "样张太大，没有画"
NOT_CARD = "样张文件不是卡片 JSON"
ON_DEVICE = "设备上的话题的样张文件 Bridge 读不到"


def has(question) -> bool:
    """Whether one of ``question``'s options carries a sample (its path or what ``carry`` kept)."""
    return isinstance(question, dict) and any(
        isinstance(option, dict) and option.get(FIELD) for option in question.get("options") or ())


def any_of(questions) -> bool:
    """Whether one of ``questions`` has an option with a sample (``has``)."""
    return any(has(question) for question in questions if isinstance(question, dict)) if isinstance(questions, list) else False


def kept(option) -> dict:
    """What ``carry`` kept of ``option``'s sample: {"elements", "colors"} or {"note"}; {} without one (or one not carried)."""
    value = option.get(FIELD) if isinstance(option, dict) else None
    return value if isinstance(value, dict) and ("elements" in value or "note" in value) else {}


def _title(header) -> list:
    """A ``header``'s title and subtitle as one markdown element: the sample's title bar, which a card in a card cannot have."""
    if not isinstance(header, dict):
        return []
    words = [str((header.get(key) or {}).get("content") or "").strip() for key in ("title", "subtitle")
             if isinstance(header.get(key), dict)]
    title, sub = (words + ["", ""])[:2]
    content = "\n".join(part for part in (f"**{title}**" if title else "", f"<font color='grey'>{sub}</font>" if sub else "")
                        if part)
    return [{"tag": "markdown", "content": content}] if content else []


def inert(value, index: int, counter=None):
    """``value`` (an element or a list of them) as sample ``index`` draws it, unable to do anything (module docstring)."""
    counter = counter if counter is not None else [0]
    if isinstance(value, list):
        return [inert(child, index, counter) for child in value]
    if not isinstance(value, dict):
        return value
    tag = value.get("tag")
    if tag == "img" and _FAKE_IMAGE.search(str(value.get("img_key") or "")):
        return {"tag": "markdown", "content": IMAGE_MARK}
    if tag == "form":
        return {"tag": "column_set", "columns": [{"tag": "column", "width": "weighted", "weight": 1,
                                                  "elements": inert(value.get("elements") or [], index, counter)}]}
    out = {}
    for key, child in value.items():
        # A button's callback ``value`` (an option's is a string); a key the Bridge never sends (``_ask``, cards/ui.py).
        if key in DROPPED or key == "value" and isinstance(child, dict) or key.startswith("_"):
            continue
        if key in ("element_id", "name") and isinstance(child, str):
            counter[0] += 1
            out[key] = f"s{index}{'x' if key == 'element_id' else 'n'}{counter[0]}"
        else:
            out[key] = inert(child, index, counter)
    return out


def nodes(value) -> int:
    """The tagged nodes in ``value`` (core/card_limits.py element_count, which cards/elements.py names too)."""
    return card_limits.element_count(value)


def size(value) -> int:
    return card_limits.json_size(value)


def elements_of(data):
    """(elements, header, colours) of what a sample file holds: a card, {"elements": [...]} or a list of elements; None
    for anything else."""
    if isinstance(data, list):
        return (data, None, {}) if data else None
    if not isinstance(data, dict):
        return None
    body = data.get("body")
    elements = body.get("elements") if isinstance(body, dict) else data.get("elements")
    if not isinstance(elements, list) or not elements:
        return None
    style = (data.get("config") or {}).get("style") if isinstance(data.get("config"), dict) else None
    colors = style.get("color") if isinstance(style, dict) else None
    return elements, data.get("header"), colors if isinstance(colors, dict) else {}


def sample(data, index: int) -> dict:
    """What a question card keeps of sample ``index``, the parsed ``data`` of its file: {"elements", "colors"} or {"note"}."""
    found = elements_of(data)
    if not found or not all(isinstance(element, dict) and element.get("tag") for element in found[0]):
        return {"note": NOT_CARD}
    elements, header, colors = found
    drawn = _title(header) + inert(elements, index)
    if size(drawn) > SAMPLE_BYTES or nodes(drawn) > SAMPLE_NODES:
        return {"note": f"{TOO_BIG}（{size(drawn) // 1000} KB、{nodes(drawn)} 个元素，一版最多 "
                        f"{SAMPLE_BYTES // 1000} KB、{SAMPLE_NODES} 个：只放要比的那一块）"}
    kept_colors = {str(name): value for name, value in list(colors.items())[:COLORS]
                   if isinstance(value, dict) and re.fullmatch(r"[\w-]{1,40}", str(name))}
    return {"elements": drawn, **({"colors": kept_colors} if kept_colors else {})}


def read(path, index: int) -> dict:
    """Sample ``index`` from the file at ``path`` (``sample``); {"note"} when it cannot be read. Never raises."""
    if not isinstance(path, str) or not Path(path).is_absolute():
        return {"note": NOT_ABSOLUTE}
    try:
        file = Path(path)
        if file.stat().st_size > FILE_BYTES:
            return {"note": f"{TOO_BIG}（文件 {file.stat().st_size // 1000} KB，最多 {FILE_BYTES // 1000} KB）"}
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return {"note": NOT_READ}
    return sample(data, index)


def carry(questions, *, device=False) -> list:
    """``questions`` with each option's ``sample`` path replaced by what the card draws of it (``read``): the first MOST
    options with one, on a single-choice question only (the owner picks one of them); the others' taken off. ``device``: the
    files are on a device the Bridge cannot read. Questions without samples are given back as they are."""
    out = []
    for question in questions:
        if not has(question):
            out.append(question)
            continue
        single = not (question.get("multiple") or question.get("multiSelect"))
        drawn, options = 0, []
        for option in question.get("options") or ():
            if not isinstance(option, dict) or not option.get(FIELD):
                options.append(option)
                continue
            rest = {key: value for key, value in option.items() if key != FIELD}
            if not single or drawn >= MOST:
                options.append(rest)
                continue
            path = option[FIELD]
            already = kept(option)
            options.append({**rest, FIELD: already or ({"note": ON_DEVICE} if device else read(path, drawn))})
            drawn += 1
        out.append({**question, "options": options})
    return out
