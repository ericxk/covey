"""A process panel of many very short steps: small in bytes, large in what CardKit counts (cards/trace_view.py TRACE_COST
and WRAPPER_NODES, app/task_card.py panel_room)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from support.cards import sample_job, stream_runtime
from covey.app.task_card import PANEL_FLOOR, panel_room
from covey.cards.card_cost import CARD_COST_BUDGET, card_cost, text_cost
from covey.cards.elements import CARD_NODES, element_count
from covey.cards.streaming import build_stream_card
from covey.cards.trace_updates import panel_json
from covey.cards.trace_view import TRACE_COST, TRACE_ELEMENTS, TraceView
from covey.core.trace import ToolTrace

START = 1_000_000.0


def steps(n, detail="", note="好。", every=1):
    """A trace of ``n`` commands of ``detail``, each followed by a progress note ``note`` every ``every`` steps."""
    trace = ToolTrace()
    for i in range(n):
        event = {"id": f"s{i}", "name": "bash", "detail": detail, "started_at": START + i}
        trace.accept("tool", {**event, "status": "running"})
        trace.accept("tool", {**event, "status": "done", "ended_at": START + i + 0.1})
        if note and i % every == 0:
            trace.accept("progress_note", {"id": f"n{i}", "text": note, "received_at": START + i + 0.5})
    return trace


class PanelSizeTests(unittest.TestCase):
    def test_many_very_short_steps_stay_within_cardkits_nodes_and_size(self):
        # 300 bare steps with a one-word note after each filled 118 nodes (the wrapper's 8 were counted as 4); 500 with
        # "x" cost 202,000 of the card's 300,000 in 23 KB.
        for n, detail in ((300, ""), (500, "x")):
            trace = steps(n, detail)
            for running in (True, False):
                if not running:
                    trace.finish()
                panel = panel_json(TraceView(trace.snapshot()).panel_model(running))
                with self.subTest(n=n, running=running):
                    self.assertLessEqual(element_count(panel), TRACE_ELEMENTS)
                    self.assertLessEqual(card_cost(panel), TRACE_COST)
                    card = build_stream_card("结论", "t", stream_runtime(), job=sample_job(), include_stop_action=True)
                    card["body"]["elements"].append(panel)
                    self.assertLessEqual(element_count(card), CARD_NODES)

    def test_an_ordinary_panel_is_drawn_as_before(self):
        snapshot = steps(60, "python3 -B -m unittest test_x", "现在看一下测试结果，然后改下一处。" * 3, every=6).snapshot()
        self.assertEqual(TraceView(snapshot).panel_model(True), TraceView(snapshot).panel_model(True, cost=10 ** 9))

    def test_a_panel_drawn_after_a_long_body_yields_to_it(self):
        body = "1\n" * 1000  # 1,000 lines: about 200,000 of the card's size.
        progress = SimpleNamespace(others={"card": 2000, "current_question": 6000, "process_panel": 4000},
                                   last_content=body)
        room = panel_room(progress)
        self.assertEqual(room, CARD_COST_BUDGET - 8000 - text_cost(body))
        panel = panel_json(TraceView(steps(500, "x").snapshot()).panel_model(True, cost=room))
        self.assertLessEqual(8000 + text_cost(body) + card_cost(panel), CARD_COST_BUDGET)
        self.assertEqual(panel_room(SimpleNamespace(others={"card": 2000}, last_content="短的回答")), TRACE_COST)
        self.assertEqual(panel_room(SimpleNamespace(others={"card": 2000}, last_content="1\n" * 5000)), PANEL_FLOOR)
        self.assertEqual(panel_room(SimpleNamespace()), TRACE_COST)  # A card without the bookkeeping.


if __name__ == "__main__":
    unittest.main()
