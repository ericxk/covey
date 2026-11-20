"""Keep the process panel mounted while its content changes.

The process panel is one boxed collapsible, 执行过程 (``trace_details``; ``panel_json`` draws it
from TraceView.panel_model): its first line inside has the count and the figures (``trace_metrics``), then the
records (``trace_body``; a card of an older release has page controls before them, which a redraw drops),
each replaced by its ID.
Before, the figures were a line above the collapsible, titled 查看过程记录; a panel of that form still diffs by
the same IDs."""
import copy

from covey.cards.cardkit_actions import delete, insert, update
from covey.cards.elements import collapsible, grey
from covey.cards.trace_view import panel_metrics, process_title


def panel_json(model):
    """The process panel of a TraceView.panel_model, split for live updates: the collapsible 执行过程
    (``trace_details``) with the figures line (``trace_metrics``), a page notice and the records (``trace_body``)
    inside, in a column of its own (``process_panel``)."""
    panel = collapsible("process_panel", process_title(model), copy.deepcopy(model.elements), expanded=model.expanded,
                        boxed=True)
    return _split(panel, panel_metrics(model), copy.deepcopy(model.notice), model.ended_empty)


def _split(result, figures, notice, ended_empty):
    body = {"tag": "column_set", "element_id": "trace_body", "columns": [
        {"tag": "column", "width": "weighted", "weight": 1, "elements": result["elements"]}]}
    metrics = [{"tag": "markdown", "element_id": "trace_metrics", "text_size": "notation", "content": grey(figures)}
               ] if figures else []
    result["element_id"] = "trace_details"
    # A page typed on an old card's page box that does not exist (app/interaction.py show_trace_page): its
    # notice goes above the records.
    result["elements"] = metrics + ([notice] if notice else []) + [body]
    if ended_empty:
        # An ended turn with no records (TraceView.panel_model): nothing to unfold, its title and figures as a
        # line (CardKit takes the collapsible replaced by a markdown element of the same ID).
        title = result["header"]["title"]["content"]
        result = {"tag": "markdown", "element_id": "trace_details",
                  "content": title + (f"\n{grey(figures)}" if figures else "")}
    # Isolate client expansion state from changing metrics and process content: the collapsible stays
    # mounted, its contents are replaced by their IDs.
    return {"tag": "column_set", "element_id": "process_panel", "columns": [
        {"tag": "column", "width": "weighted", "weight": 1, "elements": [result]}]}


def parts(panel):
    """(the figures line above the collapsible or None, the collapsible) of a split panel: the line is
    above it on a panel drawn by an older release, inside it now."""
    elements = panel["columns"][0]["elements"]
    return (elements[0] if len(elements) > 1 else None), elements[-1]


def mutable_elements(panel):
    metrics, details = parts(panel)
    return {e["element_id"]: e for e in [*([metrics] if metrics else []), *details.get("elements", [details])]}


def incremental_actions(previous, panel):
    (old_metrics, old_details), (new_metrics, new_details) = parts(previous), parts(panel)
    if (old_metrics is None) != (new_metrics is None):
        # A panel of the older form (its figures above the collapsible), by this release: whole.
        return [update(panel)]
    if old_details.get("expanded") != new_details.get("expanded"):
        # Only a replacement of the collapsible carries `expanded`. Live updates never change
        # it, so the user's own open/closed state survives them; the end of a task folds the
        # panel once (open while running, folded when finished).
        return [update(new) for old, new in ((old_metrics, new_metrics), (old_details, new_details)) if old != new]
    actions = []
    old = mutable_elements(previous)
    new = mutable_elements(panel)
    for eid, element in new.items():
        if old.get(eid) == element:
            continue
        if eid in old:
            actions.append(update(element))
        else:
            # The figures line first, before what the collapsible showed first; the rest before the records.
            target = ("trace_body" if eid != "trace_metrics" else
                      (old_details.get("elements") or [{}])[0].get("element_id", "trace_body"))
            actions.append(insert([element], target, before=True))
    for eid in old.keys() - new.keys():
        actions.append(delete(eid))
    return actions
