"""docs/ui.md's shared look: core/look.py's table, tone and band colours, cards/ui.py's parts and who stopped a job."""
from __future__ import annotations

import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

from covey.cards import board, ui
from covey.cards.elements import walk
from covey.core import jobs, look

REPO = Path(__file__).resolve().parents[1]


class TableTest(unittest.TestCase):
    def test_the_board_states_read_as_the_table(self):
        """The 看板's five states (cards/board.py LOOKS) are the table's words and colours, so it moves here unchanged."""
        for key, (word, color, tint) in board.LOOKS.items():
            self.assertEqual((look.STATES[key].word, look.STATES[key].color, look.STATES[key].tint), (word, color, tint), key)

    def test_retired_words_are_not_the_words(self):
        words = {found.word for found in look.STATES.values()}
        for key, old in look.RETIRED.items():
            self.assertIn(key, look.STATES)
            self.assertFalse(words & set(old), key)

    def test_pressing_names_states(self):
        self.assertTrue(set(look.PRESSING) <= set(look.STATES))

    def test_export_is_json_of_the_table(self):
        out = subprocess.run([sys.executable, "-B", str(REPO / "scripts" / "export_look.py")], capture_output=True,
                             text=True, check=True, timeout=60)
        data = json.loads(out.stdout)
        self.assertEqual(data, json.loads(json.dumps(look.export())))
        self.assertEqual(data["states"]["result"]["heading"], "有结果没看")
        self.assertEqual(data["states"]["stopped"]["word"], "停住了")


class ToneTest(unittest.TestCase):
    def test_statuses(self):
        cases = [("waiting_user", {}, "waiting"), ("waiting_workspace", {}, "waiting"), ("queued", {}, "queued"),
                 ("running", {}, "running"), ("running", {"stuck": True}, "stuck"), ("cancel_requested", {}, "running"),
                 ("done", {}, "result"), ("done", {"seen": True}, "done"), ("continued", {}, "result"),
                 ("failed", {}, "stopped"), ("interrupted", {}, "stopped"), ("cancelled", {}, "stopped"),
                 ("cancelled", {"by_owner": False}, "stopped"), ("cancelled", {"by_owner": True}, "cancelled"),
                 ("strange", {}, "idle"), ("", {}, "idle")]
        for status, kw, want in cases:
            self.assertEqual(look.tone(status, **kw), want, (status, kw))

    def test_who_stopped(self):
        """The owner's own stops are 已停止; the lead's, the guard's and 代答's are 停住了, as an unknown one is."""
        for by, want in (("card", "cancelled"), ("word", "cancelled"), ("web", "cancelled"), ("project", "cancelled"),
                         (jobs.RECALLED, "cancelled"),
                         ("lead", "stopped"), ("guard", "stopped"), ("proxy", "stopped"), ("", "stopped")):
            record = {"status": "cancelled", "cancel_requested_by": by}
            self.assertEqual(look.record_tone(record), want, by)
        self.assertLessEqual(look.OWNER_STOPS, jobs.STOP_SOURCES)
        self.assertEqual(look.record_tone({"status": "cancelled", "never_started": True}), "cancelled")
        self.assertEqual(look.record_tone({"status": "cancelled", "merged_into_job_id": 3}), "cancelled")
        self.assertEqual(look.record_tone({"status": "cancelled", "never_started": True, "cancel_requested_by": "lead"}),
                         "stopped")

    def test_pressing(self):
        self.assertEqual(look.pressing({"running": 3, "result": 1, "stopped": 1}), "stopped")
        self.assertEqual(look.pressing(["running", "waiting"]), "waiting")
        self.assertEqual(look.pressing({"done": 4, "queued": 1}), None)
        self.assertEqual(look.pressing({}), None)

    def test_band_steps_are_too_fine_to_see(self):
        """The owner, 2026-11-19: stripes of 3% showed; 1% did not. Each row lighter than the one above, by at most 1%,
        never under the floor; dark mode stronger; every colour has both modes."""
        colors = look.band_colors()
        alphas = [float(re.search(r",([0-9.]+)\)$", colors[f"band-{n}"]["light_mode"]).group(1))
                  for n in range(look.BAND["rows"])]
        for upper, lower in zip(alphas, alphas[1:]):
            self.assertTrue(0 <= upper - lower <= 0.0101, alphas)
        self.assertGreaterEqual(min(alphas), look.BAND["floor"])
        for name, value in colors.items():
            self.assertEqual(set(value), {"light_mode", "dark_mode"}, name)


class StopSourceTest(unittest.TestCase):
    def test_the_click_value_keeps_a_known_source_only(self):
        value = {"kind": jobs.JOB_STOP_ACTION_KIND, "job_id": "7", "message_id": "om_1"}
        self.assertNotIn("by", jobs.parse_job_stop_action_value(value))
        self.assertEqual(jobs.parse_job_stop_action_value({**value, "by": "web"})["by"], "web")
        self.assertNotIn("by", jobs.parse_job_stop_action_value({**value, "by": "somebody"}))

    def test_each_stop_names_its_source(self):
        """Each way a job is stopped other than its card's button says who: core/look.py tells the owner's apart."""
        sources = {"app/butler_tracks.py": '"by": "word"', "app/web_ops.py": '"by": "web"', "app/proxy.py": '"by": "proxy"',
                   "app/project_tools.py": 'by="lead"', "app/project_loop.py": 'by="guard"',
                   "app/project_cards.py": 'by="project"', "app/task_controls.py": 'action.get("by") in STOP_SOURCES',
                   "app/source_watch.py": "by=RECALLED"}
        for path, needle in sources.items():
            self.assertIn(needle, (REPO / "covey" / path).read_text(encoding="utf-8"), path)


class PartsTest(unittest.TestCase):
    def test_band_is_rows_a_step_lighter_inside_one_line(self):
        band = ui.conclusion_band("要你处理", ui.number(2, "件事"), "T531 等你选风格", below=ui.seg_bar({"waiting": 1}),
                                  url="https://x/y")
        self.assertEqual((band["border_color"], band["has_border"]), ("band-line", True))
        self.assertNotIn("background_style", band)  # One line, no ring around it (the owner: no blur at the edge).
        self.assertEqual([row["background_style"] for row in band["elements"]], ["band-0", "band-1", "band-2", "band-3"])
        self.assertEqual(len(band["elements"][3]["elements"]), 2)  # The segment bar and its legend: one row.
        self.assertEqual(band["behaviors"][0]["default_url"], "https://x/y")
        self.assertFalse([element for _, _, element in walk([band]) if element.get("tag") == "img"])
        short = ui.conclusion_band("要你处理", "都在掌握中")
        self.assertEqual(len(short["elements"]), 2)
        self.assertTrue(short["elements"][1]["padding"].endswith(f"{ui.EDGE} {ui.SIDE}"))

    def test_styled_names_every_colour_the_band_uses(self):
        card = ui.styled({"schema": "2.0", "config": {"style": {"color": {"mine": {"light_mode": "rgba(0,0,0,1)",
                                                                                     "dark_mode": "rgba(0,0,0,1)"}}}},
                          "body": {"elements": [ui.conclusion_band("看板", "2 件事", "x", below=[ui.todo_bar("y")])]}})
        named = card["config"]["style"]["color"]
        self.assertIn("mine", named)
        used = {element.get(key) for _, _, element in walk(card["body"]["elements"])
                for key in ("background_style", "border_color")} - {None, "bg-white"}
        self.assertTrue(used <= set(named), used - set(named))

    def test_seg_bar_is_as_long_as_its_counts(self):
        bar, legend = ui.seg_bar({"waiting": 1, "stopped": 1, "result": 1, "running": 3})
        self.assertEqual(bar["content"].count(ui.SEG_CHAR), ui.SEG_WIDTH)
        self.assertEqual(bar["content"].count(ui.SEG_CHAR * 12), 1)  # 3 of 6: half of it.
        self.assertIn("1 停住了", legend["content"])
        self.assertEqual(ui.seg_bar({"waiting": 0}), [])

    def test_row_escapes_what_it_is_given(self):
        row = ui.topic_row("stopped", "T526 a*b", "测试没过 <x>", ["23:10 停住"], url="https://x/y")
        text = json.dumps(row, ensure_ascii=False)
        self.assertNotIn("a*b", text)
        self.assertNotIn("<x>", text)
        self.assertIn("停住了", text)
        self.assertEqual(row["behaviors"][0]["default_url"], "https://x/y")


    def test_receipt_is_one_grey_line_and_a_problem_stays(self):
        self.assertEqual(ui.receipt("✓ 已停用"), {"tag": "markdown", "text_size": "notation",
                                                 "content": "<font color='grey'>已停用</font>"})
        self.assertEqual(ui.receipt("⚠️ 没有保存")["content"], "⚠️ 没有保存")
        self.assertNotIn("text_size", ui.receipt("⚠️ 没有保存"))


class BoardTest(unittest.TestCase):
    def test_a_stop_is_grouped_by_who_stopped_it(self):
        """The 看板: the lead's stop is 停住了, the owner's own 已停止 in the fold; a row without ``tone`` reads as before."""
        done = [{"topic": "a", "status": "cancelled", "tone": "stopped"},
                {"topic": "b", "status": "cancelled", "tone": "cancelled"},
                {"topic": "c", "status": "cancelled"}, {"topic": "d", "status": "done", "tone": "result"},
                {"topic": "e", "status": "done", "seen": True}]
        found = board.groups({"done": done})
        self.assertEqual({key: [row["topic"] for row in rows] for key, rows in found.items() if rows},
                         {"stopped": ["a", "c"], "cancelled": ["b"], "result": ["d"], "seen": ["e"]})
        card = json.dumps(board.board_card({"done": done, "waiting": [], "running": [{"topic": "f", "stuck": True}]}),
                          ensure_ascii=False)
        self.assertIn("做完 1 个 · 已停止 1 个", card)
        # 要你处理: the two stopped and the one that may be stuck; the band leads with 停住了, the most pressing there is.
        self.assertIn("**3**</font> **件事**", card)
        self.assertIn("先看：停住了", card)

    def test_the_title_bar_counts_as_the_band_and_rows_open_what_they_say(self):
        """看板走查 4, 5, 11: the title bar's tags count as the band (one that may be stuck is not also 在做); a folded row
        is a link; a row without a link says why."""
        view = {"done": [{"topic": "s", "label": "T2", "status": "done", "tone": "done", "url": "https://x/2"}],
                "waiting": [], "running": [{"topic": "r", "stuck": True, "url": "https://x/r", "card": "om_r"},
                                           {"topic": "q", "label": "T3"}]}
        card = board.board_card(view)
        self.assertEqual([tag["text"]["content"] for tag in card["header"]["text_tag_list"]], ["1 可能卡住", "1 在做"])
        text = json.dumps(card, ensure_ascii=False)
        self.assertIn("[T2](https://x/2)", text)
        self.assertIn(board.NO_LINK, text)
        self.assertIn("去任务卡", text)  # The one that may be stuck.

    def test_a_goal_in_paragraphs_is_one_line_on_the_board(self):
        """T545's dark screenshot (2026-11-19): a goal with blank lines read \\n\\n on the 看板."""
        card = json.dumps(board.board_card({"projects": [{"name": "Covey", "goal": "收进专项。\n\n目标：\n1. 定稿"}]}),
                          ensure_ascii=False)
        self.assertIn("目标：收进专项。 目标： 1&#46; 定稿", card)
        self.assertNotIn("\\\\n", card)

    def test_wait_words_are_whole_minutes(self):
        from covey.core.turn_words import wait_words
        self.assertEqual([wait_words(s) for s in (30, 2728, 7500, 2 * 86400 + 7300)],
                         ["不到 1 分钟", "45 分钟", "2 小时 5 分钟", "2 天 2 小时"])

    def test_the_band_says_all_is_in_hand_when_nothing_asks(self):
        card = json.dumps(board.board_card({"done": [{"topic": "d", "status": "done", "tone": "result", "label": "T1"}]}),
                          ensure_ascii=False)
        self.assertIn("**都在掌握中**", card)
        self.assertIn("1 有结果", card)


if __name__ == "__main__":
    unittest.main()
