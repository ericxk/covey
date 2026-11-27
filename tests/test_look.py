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
            self.assertIn(key, set(look.STATES) | {"revisit"}, key)  # "revisit": rows of 到点回来看, not a state
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
        # The retired words go out with it: the web page's test reads them from look.json, not from a copy of its own.
        self.assertEqual(data["retired"], {key: list(words) for key, words in look.RETIRED.items()})

    def test_web_copy_is_the_export(self):
        """web/client/src/cv/look.json is this export, re-run when core/look.py changes (scripts/export_look.py --out)."""
        copy = REPO / "web" / "client" / "src" / "cv" / "look.json"
        if not copy.exists():
            self.skipTest("no web/ in this tree")
        self.assertEqual(json.loads(copy.read_text(encoding="utf-8")), json.loads(json.dumps(look.export())),
                         "web/client/src/cv/look.json is stale: python3 -B scripts/export_look.py --out web/client/src/cv/look.json")


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
                   "app/project_clicks.py": 'by="project"', "app/task_controls.py": 'action.get("by") in STOP_SOURCES',
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


def buttons_of(card):
    """The callback values of every button in ``card``."""
    found = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("tag") == "button":
                found.extend(b.get("value") for b in node.get("behaviors") or () if b.get("type") == "callback")
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    walk(card)
    return found


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

    def test_what_may_be_stuck_comes_right_after_what_stopped(self):
        """B4 (T647): 可能卡住 is a group of its own right after 停住了 (docs/ui.md 5.1), not the end of 在做. What asks for him is
        listed whoever's it is (a live project's topic that stopped or may be stuck too); its other topics are not."""
        view = {"waiting": [], "stale": [],
                "running": [{"topic": "r", "label": "T2", "title": "在跑"},
                            {"topic": "s", "label": "T3", "title": "卡住", "stuck": True, "card": "om_s", "url": "https://x/s"},
                            {"topic": "m", "label": "T5", "title": "专项里卡住", "stuck": True},
                            {"topic": "n", "label": "T6", "title": "专项里在跑"}],
                "done": [{"topic": "f", "label": "T1", "status": "failed", "tone": "stopped"},
                         {"topic": "p", "label": "T7", "status": "failed", "tone": "stopped"},
                         {"topic": "d", "label": "T4", "status": "done", "tone": "result"}],
                "projects": [{"name": "迁库", "topics": ["m", "n", "p"], "running": 1, "stuck": 1, "stopped": 1}]}
        card = board.board_card(view)
        headings = [element["content"].split("**")[1] for element in card["body"]["elements"]
                    if element.get("tag") == "markdown" and element.get("icon")]
        self.assertEqual(headings, ["停住了", "可能卡住", "有结果没看", "在做", "专项"])
        text = json.dumps(card, ensure_ascii=False)
        for number, title in (("T1", ""), ("T7", ""), ("T3", "**卡住**"), ("T5", "**专项里卡住**"), ("T2", "**在跑**")):
            self.assertIn(f"<font color='white'>**{number}**</font>", text)  # Its badge (cards/ui.py topic_row, T657 B6).
            self.assertIn(title, text)
        self.assertNotIn("T6", text)  # A live project's topic at work: its block counts it.
        self.assertIn("1 个停住了、1 个可能卡住、1 个在做", text)
        self.assertEqual([tag["text"]["content"] for tag in card["header"]["text_tag_list"]], ["2 停住了", "2 可能卡住", "1 在做"])
        self.assertIn("**4**</font> **件事**", text)

    def test_a_project_a_guard_paused_asks_for_him_and_one_he_stopped_does_not(self):
        """P0-3 (T647): 停住了 and why in red, counted among 要你处理 and led with when nothing else asks (its 继续 is on the pause
        card: the 看板's row buttons are gone); the owner's own stop is 已停止, background; a folded one 收起."""
        view = {"projects": [{"id": "a1b2c3d4e5f6", "name": "迁库", "topics": [], "halted": "token 用完了", "url": "https://x/p"},
                             {"id": "b2c3d4e5f6a7", "name": "周报", "topics": [], "cancelled": True},
                             {"id": "c3d4e5f6a7b8", "name": "旧的", "topics": [], "folded": True}]}
        card = board.board_card(view)
        text = json.dumps(card, ensure_ascii=False)
        self.assertIn("**1**</font> **件事**", text)
        self.assertIn("先看：停住了 · 专项「迁库」", text)
        self.assertIn("<font color='red'>**停住了**</font><font color='grey'> · token 用完了</font>", text)
        self.assertIn("<font color='grey'>**已停止**</font>", text)
        go = [button for button in buttons_of(card) if button.get("kind") == board.PROJECT_GO_KIND]
        self.assertEqual(go, [])
        self.assertEqual([tag["text"]["content"] for tag in card["header"]["text_tag_list"]], ["1 停住了"])
        self.assertIn("旧的", text)
        self.assertIn("收起", text)

    def test_a_crowded_board_fits_what_cardkit_takes_and_still_counts_everything(self):
        """P0-10 (T647): with every group full (50 projects on, folded ones piling up), the card shows fewer rows a group
        (board.FIT_SHOWN) until CardKit takes it (cards/elements.py CARD_NODES, CARD_BYTES); the band counts them all."""
        from covey.cards.elements import CARD_BYTES, CARD_NODES, element_count, json_size
        long = "很长的标题" * 6

        def rows(prefix, count, **fields):
            return [{"topic": f"{prefix}{n}", "label": f"T{n}", "title": long, "url": f"https://x/{prefix}{n}",
                     "card": f"om_{prefix}{n}", "line": "现在：" + long, "meta": ["3/7 步", "2 分钟前有动静"], **fields}
                    for n in range(count)]
        view = {"waiting": rows("w", 12), "stale": rows("s", 5), "dismissable": True, "can_dismiss": True,
                "running": rows("r", 12) + rows("k", 12, stuck=True),
                "done": rows("f", 12, status="failed", tone="stopped") + rows("d", 12, status="done", tone="result")
                + rows("c", 20, status="cancelled", tone="cancelled", state="已停止"),
                "revisits": [{"topic": f"v{n}", "label": f"T{n}", "title": long, "at": "15:30", "look": long, "set_at": n}
                             for n in range(12)],
                "projects": [{"id": f"{n:012x}", "name": long, "goal": long, "topics": [], "waiting": 1, "running": 2,
                              **({"halted": "很久没有动静"} if n % 5 == 0 else {})} for n in range(50)]
                + [{"id": f"f{n:011x}", "name": long, "topics": [], "folded": True} for n in range(60)],
                "pending": [{"id": f"p{n}", "name": long, "goal": long, "rows": rows("pr", 3)} for n in range(12)],
                "tidy": {}, "stamp": "20:59"}
        card = board.board_card(view)
        self.assertLessEqual(element_count(card), CARD_NODES)
        self.assertLessEqual(json_size(card), CARD_BYTES)
        text = json.dumps(card, ensure_ascii=False)
        # 等你 29 (12, the 5 old and the 12 proposals: T657 B3), 停住了 12 and the 10 projects a guard paused, 可能卡住 12.
        self.assertIn("**63**</font> **件事**", text)
        self.assertEqual([tag["text"]["content"] for tag in card["header"]["text_tag_list"]], ["29 等你", "22 停住了", "12 可能卡住"])
        self.assertIn("另有", text)  # What is left out is said in one line.
        # A board that fits is drawn whole as before: every row. (A row with its T number's badge, T657 B6, costs more than
        # one without: two groups of SHOWN rows this long no longer fit, two of six do.)
        few = {"waiting": rows("w", 6), "running": rows("k", 6, stuck=True), "done": [], "stale": []}
        drawn = json.dumps(board.board_card(few), ensure_ascii=False)
        self.assertNotIn("另有", drawn)
        self.assertEqual(drawn.count("去任务卡"), 6)
        full = {"waiting": rows("w", board.SHOWN), "running": rows("k", board.SHOWN, stuck=True), "done": [], "stale": []}
        drawn = board.board_card(full)
        self.assertLessEqual(element_count(drawn), CARD_NODES)
        self.assertLessEqual(json_size(drawn), CARD_BYTES)
        self.assertIn("另有", json.dumps(drawn, ensure_ascii=False))  # Fewer a group, the rest one line.

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

    def test_an_empty_card_tells_a_first_use_from_a_record_from_a_clear_board(self):
        """T604 #53: 都在掌握中 only for a board that is clear now; the others say what they are."""
        first = board.board_card({"first_use": True})
        text = json.dumps(first, ensure_ascii=False)
        self.assertIn("**还没有话题**", text)
        self.assertIn("第一个话题从这里开始", text)
        self.assertNotIn("都在掌握中", text)
        self.assertEqual([tag["text"]["content"] for tag in first["header"]["text_tag_list"]], ["还没有话题"])
        # A first use with something listed (or a project) is not one.
        self.assertIn("**都在掌握中**", json.dumps(board.board_card({"first_use": True, "running": [{"label": "T1"}]}), ensure_ascii=False))
        old = {"at": 1000.0, "stamp": "10:05"}
        self.assertIn("**都在掌握中**", json.dumps(board.board_card(old), ensure_ascii=False))  # Not told the time: as before.
        self.assertIn("**都在掌握中**", json.dumps(board.board_card(old, 1000.0 + board.SNAPSHOT_AGE), ensure_ascii=False))
        later = json.dumps(board.board_card(old, 1000.0 + board.SNAPSHOT_AGE + 1), ensure_ascii=False)
        self.assertIn("**10:05 时没有等你的事**", later)
        self.assertIn("这是 10:05 的记录，不是现在的；点刷新看现在的", later)
        self.assertNotIn("都在掌握中", later)


if __name__ == "__main__":
    unittest.main()


class ColourRulesTest(unittest.TestCase):
    """docs/ui.md 2.2 (T604 #50), over every card sample the golden file has, as the builders draw them now
    (tests/test_card_golden.py render): orange says 等你 (and what waits for the owner's choice) and 可能卡住 alone; no red
    text sits on a red ground, there being no red ground at all."""

    FONT = re.compile(r"<font color='(orange|red)'>(.*?)</font>")
    TAG = re.compile(r"<text_tag color='(orange|red)'>(.*?)</text_tag>")
    # What orange may say: the two states (with their count), a group or a choice waiting for the owner, the state's dot and
    # segment of the 看板's bar, a 要你做 bar's label (cards/ui.py todo_bar), and the icons of those states.
    ORANGE_WORDS = re.compile(r"^\**(\d+ )?(等你|可能卡住|等你确认|要你确认)\**$|^\d+ 条建议$|^\d+ 个匹配$|^●$|^━+$|^\*\*要\S{1,3}\*\*$")
    ORANGE_ICONS = {"time_outlined", "warning_outlined", "chat_outlined"}

    @classmethod
    def setUpClass(cls):
        from test_card_golden import render

        cls.samples = json.loads(json.dumps(render(), ensure_ascii=False))

    def uses(self):
        samples = self.samples
        found = []

        def visit(node, sample):
            if isinstance(node, dict):
                if node.get("tag") == "text_tag" and node.get("color") in ("orange", "red"):
                    found.append((sample, node["color"], "words", str((node.get("text") or {}).get("content"))))
                if node.get("tag") == "standard_icon" and node.get("color") in ("orange", "red"):
                    found.append((sample, node["color"], "icon", str(node.get("token"))))
                if str(node.get("background_style") or "").startswith(("orange", "red", "carmine")) or \
                        str(node.get("template") or "") in ("orange", "red", "carmine"):
                    found.append((sample, "ground", "ground", str(node.get("background_style") or node.get("template"))))
                for value in node.values():
                    if isinstance(value, str):
                        found.extend((sample, color, "words", text) for color, text in
                                     self.FONT.findall(value) + self.TAG.findall(value))
                    else:
                        visit(value, sample)
            elif isinstance(node, list):
                for value in node:
                    visit(value, sample)
        for sample, card in samples.items():
            visit(card, sample)
        return found

    def test_orange_says_waiting_or_maybe_stuck_alone(self):
        off = [(sample, kind, text) for sample, color, kind, text in self.uses() if color == "orange" and not (
            self.ORANGE_WORDS.match(text) if kind == "words" else text in self.ORANGE_ICONS)]
        self.assertEqual(off, [])

    def test_no_red_ground_so_no_red_text_on_one(self):
        self.assertEqual([use for use in self.uses() if use[1] == "ground"], [])

    def test_the_cards_it_was_said_by_say_it_so(self):
        from covey.cards import group_manager, proxy_panel, status_card, workspace_manager
        from covey.cards.project_card import ask_card

        head = ask_card("迁库", "62 分钟没有进展", project_id="p", lead_topic="t")["header"]
        self.assertEqual((head["text_tag_list"][0]["text"]["content"], head["text_tag_list"][0]["color"]), ("停住了", "red"))
        self.assertEqual(group_manager.state_words({"chat_id": "oc_x"}), ("未确认", "neutral"))
        self.assertIn("<text_tag color='blue'>处理中 2</text_tag>", proxy_panel.state_tags(0, 0, 2))
        self.assertEqual(workspace_manager.tag_words({"name": "w", "state": None}, "d"), [("未确认", "neutral")])
        self.assertIn("color='orange'>等你<", status_card.status_tag("等你"))
        self.assertIn("color='red'>停住了<", status_card.status_tag("停住了"))
