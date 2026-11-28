"""Reading card JSON: element walks, launch-card forms and a sample task;
saving the browse-card address a sent card leaves behind."""
from __future__ import annotations

import json
import re
from types import SimpleNamespace

TOPIC = "a" * 64


def text_of(card):
    """The card as one string, Chinese unescaped, for ``assertIn`` on what it says."""
    return json.dumps(card, ensure_ascii=False)


def remember_browse_card(app, ctx, message_id, card_id, chat_id="oc_chat", **fields):
    """The address ``publish_card`` saves for a sent card (message -> card), which repaints use."""
    path = ctx.state_dir / "browse-cards" / (app.voice.safe_key(message_id) + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"topic_id": ctx.topic_id, "chat_id": chat_id, "card_id": card_id, "sequence": 0,
                                **fields}))
    return path


def form_button(form, name):
    """The button of ``form`` named ``name``, at any depth (提交回答 and 只发补充说明 share a row)."""
    return next(e for e in walk_dicts(form) if e.get("tag") == "button" and e.get("name") == name)


def md_text(element):
    """The text of a markdown element, or of the one in a block around it (a 进展说明 on its blue block)."""
    return next(e["content"] for e in walk_dicts(element) if e.get("tag") == "markdown")


def walk_dicts(value):
    """Every dict nested anywhere in ``value``, parents first."""
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_dicts(child)


def walk_elements(elements):
    """Card elements in order, descending into ``elements`` and column ``elements``."""
    for element in elements:
        yield element
        yield from walk_elements(element.get('elements', []))
        for column in element.get('columns', []):
            yield from walk_elements(column.get('elements', []))


def settings_rows(card):
    """A launch or 换配置 card's settings rows (cards/launch_config.py setting_row), by key: ``current`` (the option
    after ✓), ``options`` ((value, text) pairs), ``taps`` (each option's callback value) and ``open``."""
    rows = {}
    for e in walk_elements(card['body']['elements']):
        if e.get('tag') != 'collapsible_panel' or not str(e.get('element_id', '')).startswith('set_'):
            continue
        row = rows[e['element_id'][4:].rpartition('_')[0]] = {'current': None, 'options': [], 'taps': {},
                                                               'open': e['expanded']}
        for line in walk_elements(e['elements']):
            if line.get('tag') != 'interactive_container':
                continue
            value = line['behaviors'][0]['value']
            text = next(t['content'] for t in walk_elements(line['elements']) if t.get('tag') == 'markdown')
            if text.startswith('✓ '):
                row['current'] = value['option']
            shown = re.sub(r"^✓ \*\*<font color='blue'>(.*)</font>\*\*$", r'\1', text).removeprefix('\u3000 ')
            row['options'].append((value['option'], shown))
            row['taps'][value['option']] = value
    return rows


def page_options(card, key=None):
    """The (value, text) options of the row ``key``, or of the row drawn open."""
    rows = settings_rows(card)
    return rows[key]['options'] if key else next(row['options'] for row in rows.values() if row['open'])


def card_selects(card):
    """Every setting on a launch card, as the select it replaced: its name and initial_option (the row's current)."""
    return [{'name': key, 'initial_option': row['current']} for key, row in settings_rows(card).items()]


def form_fields(card):
    form = next(e for e in card['body']['elements'] if e.get('tag') == 'form')
    return {e['name']: e for e in walk_elements(form['elements']) if e.get('tag') in {'select_static', 'input'}}


def selects(card):
    """card_selects by name: each records its choice by callback."""
    return {e['name']: e for e in card_selects(card)}


def fill(card, **values):
    """Submit only what the published card's form can actually send."""
    missing = set(values) - set(form_fields(card))
    if missing:
        raise AssertionError(f'card has no form field for {sorted(missing)}')
    return values


def current_token(state_dir, token):
    """The form a launch card shows now: a model change moves it to a new token."""
    seen = set()
    while token not in seen:
        seen.add(token)
        record = json.loads((state_dir / 'launch-forms' / (token + '.json')).read_text())
        if record.get('status') != 'replaced':
            return token
        # Fixtures may reuse a message id across topics: only this form's replacement is its next card.
        token = record['replaced_by']
    raise AssertionError('cycle in launch form replacements: ' + token)


def stream_runtime():
    """A StreamingCard with inert callbacks, for building stream cards directly."""
    from covey.app.task_card import StreamingCard
    return StreamingCard("t", "t", "answer", lambda **kw: kw, lambda _: None, lambda s: s, lambda *a: True,
                         lambda s, n: s[:n], lambda *a, **k: None, lambda *a, **k: (None, False),
                         update_interval=0, replacement_limit=0)


def sample_job(**fields):
    """A running Codex chat task as the task card sees it."""
    from covey.core.harness import HarnessCapabilities
    values = dict(job_id=431, user_text="列出当前目录前 3 个文件名\n" * 50, topic_id=TOPIC, topic_label="T70",
                  message_id="om_task", chat_id="chat", reply_to_message_id="om_root", harness_id="codex",
                  binding_revision=2, model="gpt-6-astra", effort="xhigh", timeout=0, kind="chat", title="",
                  harness_capabilities=HarnessCapabilities(steering=True),
                  # The 输入/输出 box's 输入 (app/cards.py card_facts, app/answer_cards.py inputs).
                  input_parts=["Codex", "Codex 自带", "GPT-6-Astra", "思考 xhigh", "脱敏关", "demo-app"])
    values.update(fields)
    return SimpleNamespace(**values)


def panel_title(panel):
    """A process panel's title as it read before 2026-11-04 ("执行过程 · 3 步" and its figures under it), from
    the grey collapsible's title and its figures line (TraceView.panel ``metrics``): a running panel's
    title says 在做 (进行中 before) and the line has the count first."""
    import re

    plain = re.sub(r"</?font[^>]*>|\*\*", "", panel["header"]["title"]["content"])
    name, _, state = plain.partition("\u3000")
    lines = [line for line in str(panel.get("metrics", "")).split("\n") if line]
    if state in ("在做", "进行中") and lines:
        state, lines = lines[0], lines[1:]
    return "\n".join([name + (f" · {state}" if state else "")] + lines)


def trace_panel(trace, running=True, *, timed=False):
    """The process panel of ``trace`` (a ToolTrace or a TraceView) as one collapsible, for tests that read its title
    and records; ``metrics`` and ``no_records`` beside them are what ``split_panel`` needs to draw it as
    cards/trace_updates.py ``panel_json`` does."""
    from covey.cards.elements import collapsible
    from covey.cards.trace_view import TraceView, panel_metrics, process_title

    view = trace if isinstance(trace, TraceView) else TraceView(trace.snapshot())
    model = view.panel_model(running, timed=timed)
    panel = collapsible("process_panel", process_title(model), model.elements, expanded=model.expanded, boxed=True)
    panel["metrics"] = panel_metrics(model)
    if model.ended_empty:
        panel["no_records"] = True
    return panel


def split_panel(panel):
    """cards/trace_updates.py ``panel_json`` from the collapsible ``trace_panel`` gives, its ``metrics`` and
    ``no_records`` beside it."""
    import copy

    from covey.cards.trace_updates import _split

    result = copy.deepcopy(panel)
    notice = next((e for e in result["elements"] if e.get("element_id") == "trace_page_notice"), None)
    result["elements"] = [e for e in result["elements"] if e is not notice]
    return _split(result, result.pop("metrics", ""), notice, result.pop("no_records", False))


def figures_line(snapshot):
    """The main task's model figures as 任务详情 said them before 2026-11-09: generation time, token/s and TTFT, those
    the backend gave (none estimated); 主任务 leads when the turn had subtasks. "" when the backend gave none."""
    from covey.core.trace import has_subtasks, turn_usage
    from covey.cards.trace_view import TraceView
    from covey.core.ttft import SOURCES
    from covey.core.usage import number, rate

    if "started_at" not in snapshot:
        return ""
    usage = turn_usage(snapshot)
    view = TraceView(snapshot)
    figures = []
    if number(view.generation_seconds) and view.generation_seconds > 0:
        figures.append(f"模型生成 {view.generation_seconds:.1f} 秒")
    speed = rate(view.measured_output_tokens, view.generation_seconds)
    if speed is not None:
        measured, main = view.measured_output_tokens, usage["main"]["output_tokens"]
        figures.append(f"平均 {speed:.1f} token/s" + (f"（只算模型生成计时内的 {measured:,} token）"
                                                       if type(main) is int and measured != main else ""))
    if number(view.ttft_seconds) and view.ttft_source in SOURCES:
        figures.append(f"TTFT {view.ttft_seconds:.2f} 秒")
    if not figures:
        return ""
    return ("主任务 " if has_subtasks(usage) else "") + " · ".join(figures)


def status_block(content, figures):
    """The task card's status block of 2026-11-04 (the status line in a narrow column left of the tiles ``figures``),
    which no card draws since the 输入/输出 box (2026-11-08): kept as a golden sample of that layout."""
    from covey.cards.task_figures import STATUS_BLOCK_ID
    from covey.cards.task_status import activity_element, status_rows

    status = {"tag": "column", "width": "weighted", "weight": 1, "vertical_align": "top",
              "padding": "2px 0px 2px 0px", "vertical_spacing": "4px"}
    return {"tag": "column_set", "element_id": STATUS_BLOCK_ID, "flex_mode": "stretch", "horizontal_spacing": "12px",
            "columns": [{**status, "elements": [activity_element(status_rows(content)[0])]},
                        {"tag": "column", "width": "weighted", "weight": 3, "vertical_align": "top",
                         "elements": [figures]}]}
