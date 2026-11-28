"""The UI rules the owner settled (docs/ui.md 13 拍板记录), one test class a rule, over every entry a rule covers: the
butler (管家), a project's lead (负责人), a topic (话题), a project's topic (专项的话题) and a topic the web page opened (网页开的话题).

Each rule reads the card goldens (tests/golden/cards.json, the cards the real builders draw; test_card_golden keeps them
the code's) through a matrix that names, per entry, the samples it is checked on (``MATRIX``), so a rule is checked on
every entry and an entry whose samples go is a failure, not a silent gap; a rule the goldens cannot show (the butler's own
question card, the web page's catalog) draws live. GOLDEN_CARDS=update rewrites the goldens; it does not make these pass:
a failure here means a settled look was changed back. Changing a rule takes the owner's word in that turn, then its row
in docs/ui.md 13 and its test here (DecisionTableTest keeps the two in step).

The 看板 cards (看板, 手上的事, 早晚汇总) count from the one 等主人 ledger (core/owner_ledger.py, U12 in its own tests); here
only how they look (U11).
"""
from __future__ import annotations

import json
import lzma
import re
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import REPO  # noqa: E402
from support.world import UnifiedCase  # noqa: E402

GOLDEN = REPO / "tests" / "golden" / "cards.json"
UI_DOC = REPO / "docs" / "ui.md"
BUTLER, LEAD, TOPIC, MEMBER, WEB = "管家", "负责人", "话题", "专项的话题", "网页开的话题"

# Rule -> entry -> the golden samples it is checked on. A topic the web page opened is a topic (app/web_launch.py opens it
# as the butler opens one): its cards are a topic's, its topic card the butler's topics' (main_topic), its catalog live.
MATRIX = {
    "U1": {BUTLER: ("task.answer_card_butler", "main_task.done", "main_task.failed"),
           LEAD: ("task.answer_card_lead", "task.answer_card_lead_speaker"),
           TOPIC: ("task.running", "task.answer_card_thread", "task.answer_relay_speaker", "task.relay_stopped",
                   "task.relay_waiting", "question.card_in_thread", "question.mirror_pending", "recovery.interrupted",
                   "steer.receipt"),
           MEMBER: ("task.question_copy_member", "task.question_copy_member_settled"),
           WEB: ("main_topic.numbered",)},
    "U3": {BUTLER: ("task.answer_card_butler", "main_task.done", "main_task.failed", "main_task.stopped"),
           LEAD: ("task.answer_card_lead", "task.answer_card_lead_speaker"),
           TOPIC: ("task.answer_card_thread", "task.answer_relay_speaker", "task.relay_stopped", "task.relay_waiting",
                   "question.card_in_thread", "question.mirror_pending"),
           MEMBER: ("task.question_copy_member", "task.question_copy_member_settled")},
    "U4": {TOPIC: ("question.card_in_thread", "question.card_pending", "question.card_multi_select"),
           "顶层副本": ("question.mirror_pending", "question.card_top_pick", "question.card_top_pick_rows"),
           "负责人转来的": ("question.card_escalated", "question.card_escalated_recommend"),
           "任务卡上的": ("question.element_on_task_card",)},
    "U11": {"看板": ("digest.board", "digest.board_butler_waiting", "digest.board_stops", "digest.board_empty"),
            "手上的事": ("overview.overview_butler_waiting", "overview.overview_stopped_stuck", "overview.pinned_board"),
            "早晚汇总": ("digest.evening", "digest.three_parts")},
    "U5": {BUTLER: ("settings.config_butler",), LEAD: ("settings.config_lead", "settings.config_lead_chosen", "switch.card_lead"),
           "新话题默认": ("settings.config_topics",),
           "代答": ("proxy.panel",), WEB: ("main_topic.numbered",),
           TOPIC: ("launch.codex", "launch.claude_redacting", "launch.maso_expanded", "switch.card", "switch.card_old_record"),
           "顶层换配置": ("switch.card_top",)},
}
# The cards of a conversation (docs/ui.md 10 ③): no conclusion band on any (U1), whatever entry drew them.
CONVERSATION = ("task.", "question.", "recovery.", "steer.", "switch.", "pages.", "main_task.", "rewind.",
                "topic.")
# Samples a rule leaves alone, each with why: an old card's look kept, or a test sample that is no card the Bridge sends.
HEADER_EXEMPT = {"recovery.stopped": "测试样本把“T70 · #431”当成（模板, 标题）传给了 stopped_card；现网的停住卡不带标题栏"}
BAND_EXEMPT = {"digest.asked_folded": "再点一次菜单的看板，原来那张收成一行（app/digest.py fold_board），不再是看板"}


def golden() -> dict:
    if GOLDEN.exists():
        return json.loads(GOLDEN.read_text(encoding="utf-8"))
    return json.loads(lzma.decompress(GOLDEN.with_name("cards.json.xz").read_bytes()).decode("utf-8"))


CARDS = golden()


def cards():
    """(key, card) of every golden sample that is a card (a dict with a body)."""
    return [(key, value) for key, value in CARDS.items() if isinstance(value, dict) and isinstance(value.get("body"), dict)]


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def buttons(value) -> list:
    return [node for node in walk(value) if node.get("tag") == "button"]


def words(button) -> str:
    return str((button.get("text") or {}).get("content") or "")


def kind(button) -> str:
    for behavior in button.get("behaviors") or ():
        if behavior.get("type") == "callback":
            return str((behavior.get("value") or {}).get("kind") or "")
    return str((button.get("value") or {}).get("kind") or "") if isinstance(button.get("value"), dict) else ""


class MatrixTest(unittest.TestCase):
    """Every cell of MATRIX names samples that are there: an entry is never checked on nothing."""

    def test_every_cell_has_its_samples(self):
        for rule, entries in MATRIX.items():
            for entry, keys in entries.items():
                for key in keys:
                    with self.subTest(rule=rule, entry=entry, key=key):
                        self.assertIn(key, CARDS, f"{rule} 的 {entry} 用的样本 {key} 不在 golden 里：补回样本或改 MATRIX")

    def test_the_four_entries_are_covered(self):
        for rule in ("U1", "U3"):
            with self.subTest(rule=rule):
                self.assertLessEqual({BUTLER, LEAD, TOPIC, MEMBER}, set(MATRIX[rule]))
        self.assertLessEqual({BUTLER, LEAD, WEB}, set(MATRIX["U5"]))


class BandTest(unittest.TestCase):
    """U1: no conclusion band on a conversation's cards (task, answer, relay, question and its copy, stopped card, receipt)."""

    def test_no_band_on_a_conversation_card(self):
        from covey.cards.ui import has_band
        keys = {key for entries in MATRIX["U1"].values() for key in entries}
        for key, card in cards():
            if key.startswith(CONVERSATION) or key in keys:
                with self.subTest(key=key):
                    self.assertFalse(has_band(card["body"].get("elements")), f"{key} 有结论带（docs/ui.md 13 U1）")


class BoardBandTest(unittest.TestCase):
    """U11: the 看板, 手上的事 and 早晚汇总 open with their conclusion band, empty or not."""

    def test_every_board_card_has_its_band(self):
        from covey.cards.ui import has_band
        keys = {key for entries in MATRIX["U11"].values() for key in entries}
        for key, card in cards():
            if (key.startswith(("digest.", "overview.")) or key in keys) and key not in BAND_EXEMPT:
                with self.subTest(key=key):
                    self.assertTrue(has_band(card["body"].get("elements")), f"{key} 没有结论带（docs/ui.md 13 U11）")


class HeaderTest(unittest.TestCase):
    """U2: a title bar is the default one, never coloured; a speaker's card has none."""

    def test_title_bars_are_default(self):
        for key, card in cards():
            header = card.get("header")
            if key in HEADER_EXEMPT or not isinstance(header, dict):
                continue
            with self.subTest(key=key):
                self.assertEqual(header.get("template", "default"), "default", f"{key} 用了彩色标题栏（U2）")

    def test_a_speakers_card_has_no_title_bar(self):
        for entry, keys in MATRIX["U3"].items():
            for key in keys:
                with self.subTest(entry=entry, key=key):
                    self.assertNotIn("header", CARDS[key])

    def test_the_exempt_ones_are_still_there(self):
        for key in (*HEADER_EXEMPT, *BAND_EXEMPT):
            with self.subTest(key=key):
                self.assertIn(key, CARDS, "豁免的样本没了：从 HEADER_EXEMPT 去掉它")


class SpeakerRowTest(unittest.TestCase):
    """U3: a speaker's card opens with its speaker's row (cards/ui.py speaker_row), on every entry."""

    def test_the_row_comes_first(self):
        from covey.cards.ui import ROW_ID
        for entry, keys in MATRIX["U3"].items():
            for key in keys:
                with self.subTest(entry=entry, key=key):
                    first = CARDS[key]["body"]["elements"][0]
                    self.assertEqual(first.get("element_id"), ROW_ID, f"{entry} 的 {key} 第一个元素不是头像行（U3）")


def send_row_problems(card) -> list:
    """What breaks U4 in an open question form: its send buttons are 提交回答 (primary_filled, as wide as it can be) and
    at most 不处理 (bordered, beside it, as wide), no other button but the options' (each bordered), no retired kind."""
    from covey.cards.question_cards import ASK_KIND, PICK, SET_ASIDE_LABEL, SKIP_ALL_KIND, SUBMIT_LABEL
    found, problems = buttons(card), []
    submits = [b for b in found if words(b) == SUBMIT_LABEL]
    asides = [b for b in found if words(b) == SET_ASIDE_LABEL]
    picks = [b for b in found if any(PICK in (behavior.get("value") or {}) for behavior in b.get("behaviors") or ())]
    if len(submits) != 1 or (submits[0].get("type"), submits[0].get("width")) != ("primary_filled", "fill"):
        problems.append("提交回答要恰好一个、实心、通栏")
    if len(asides) > 1 or asides and (asides[0].get("type"), asides[0].get("width")) != ("default", "fill"):
        problems.append("不处理最多一个、边框、通栏")
    if asides:
        rows = [node for node in walk(card) if node.get("tag") == "column_set"
                and {words(b) for b in buttons(node)} == {SUBMIT_LABEL, SET_ASIDE_LABEL}]
        if not rows or [c.get("weight") for c in rows[-1]["columns"]] != [1, 1]:
            problems.append("不处理要和提交回答并排、一样宽")
    others = [b for b in found if b not in submits + asides + picks]
    if others:
        problems.append("问题卡上多了按钮：" + "、".join(words(b) for b in others))
    if any(b.get("type") not in ("default", None) for b in picks):
        problems.append("选项按钮要是边框按钮")
    if any(kind(b) in (SKIP_ALL_KIND, ASK_KIND) for b in found):
        problems.append("用了下线的只发补充说明 / 先问一句")
    return problems


class QuestionButtonsTest(unittest.TestCase):
    """U4: a question card's buttons, on every entry that shows one."""

    def test_every_entry(self):
        for entry, keys in MATRIX["U4"].items():
            for key in keys:
                with self.subTest(entry=entry, key=key):
                    self.assertEqual(send_row_problems(CARDS[key]), [])

    def test_the_butlers_own_question_has_no_set_aside(self):
        from covey.cards.question_cards import SET_ASIDE_LABEL, question_card
        from covey.core import butler
        record = {"token": "0123456789abcdef", "status": "pending", "topic_id": butler.butler_topic_id("oc_owner"),
                  "chat_id": "oc_owner", "job_id": 12, "blocking": True, "created_at": 0, "mirror_state": "at_top",
                  "questions": [{"id": "way", "question": "先做哪个？", "options": [{"label": "A"}, {"label": "B"}]}]}
        card = question_card(record)
        self.assertEqual(send_row_problems(card), [])
        self.assertNotIn(SET_ASIDE_LABEL, [words(b) for b in buttons(card)])

    def test_options_are_laid_out_by_their_width(self):
        from covey.cards.question_cards import SKIP_LABEL, pick_rows
        self.assertEqual(pick_rows(["表格", "列表", SKIP_LABEL]), [[0, 1, 2]])  # Short ones side by side.
        self.assertIsNone(pick_rows(["保留草稿，在新版组上继续跑完第 1 轮再决定（推荐）", "回退", SKIP_LABEL]))


# Words that do not say which model or budget runs (U5).
NAMELESS = re.compile(r"Default \(recommended\)|模型原生默认|推荐 ?· |推荐 (?:low|medium|high|xhigh|max)|默认模型|模型默认|思考 默认")
# Where they are still shown until the owner decides (docs/ui.md 13 还没定的), each prefix still matching a sample that
# shows them. None now: the launch card and the 换配置 card went the 设置 card's way too (the owner's choice A).
PENDING = ()


def shown_texts(card) -> list:
    """Every text a card shows: markdown, plain_text and the options of its dropdowns."""
    return [str(node.get("content")) for node in walk(card) if isinstance(node.get("content"), str)]


class ModelChoiceTest(UnifiedCase):
    """U5: a model or a budget is named by what it runs, on every entry that offers or says one."""

    def test_no_nameless_choice_outside_the_pending_ones(self):
        pending = {prefix: False for prefix in PENDING}
        for key, card in cards():
            hits = sorted({hit for text in shown_texts(card) for hit in NAMELESS.findall(text)})
            if not hits:
                continue
            prefix = next((p for p in PENDING if key.startswith(p)), None)
            if prefix:
                pending[prefix] = True
                continue
            with self.subTest(key=key):
                self.fail(f"{key} 写着 {hits}：模型和思考预算要说得出是哪一个（docs/ui.md 13 U5）")
        self.assertEqual([p for p, seen in pending.items() if not seen], [],
                         "这几处已经不写了：从 PENDING 和 docs/ui.md 13 还没定的里去掉")

    def test_every_entry_s_samples_name_theirs(self):
        for entry, keys in MATRIX["U5"].items():
            for key in keys:
                with self.subTest(entry=entry, key=key):
                    texts = shown_texts(CARDS[key])
                    self.assertTrue(texts)
                    self.assertEqual([t for t in texts if NAMELESS.search(t)], [])

    def test_the_rows_every_page_lists_are_named(self):
        from covey.core.defaults import RECOMMENDED
        from covey.core.menu_choice import budget_options, explicit, plain_budgets
        entry = {"harness": "claude", "levels": ["low", "high", "xhigh", "max"], "default": "high",
                 "presets": {"": "", "high": "high"}}
        self.assertTrue(any(NAMELESS.search(text) for _, text in budget_options(entry)[0]))  # The launch card's list.
        named = plain_budgets(entry)
        self.assertTrue(named)
        self.assertNotIn(RECOMMENDED, [value for value, _ in named])
        self.assertEqual([text for _, text in named if NAMELESS.search(text)], [])
        menu = {"claude:default": {"harness": "claude", "display_name": "Default (recommended)"},
                "claude:opus": {"harness": "claude", "display_name": "Opus 5.5"}}
        self.assertEqual(list(explicit(menu)), ["claude:opus"])

    MENU = {"claude:default": {"harness": "claude", "title": "Claude Code", "provider": "anthropic", "provider_title": "Claude 自带",
                               "display_name": "Default (recommended)", "levels": ["low", "high", "xhigh", "max"]},
            "claude:opus": {"harness": "claude", "title": "Claude Code", "provider": "anthropic", "provider_title": "Claude 自带",
                            "display_name": "Opus 5.5", "levels": ["low", "high", "xhigh", "max"], "presets": {"": ""}}}

    def test_the_launch_and_switch_rows_name_theirs(self):
        """The launch card's and 换配置's rows (cards/launch_config.py model_settings): no "Default (recommended)" unless it is
        the selected one (a topic bound to it), no 推荐 or 模型原生默认 unless it was chosen before (a topic's binding, a tap
        on a card sent before: drawn as it was, as the 设置 card draws a saved one); nothing chosen, the level 推荐 names."""
        from covey.cards.launch_config import model_settings
        from covey.core.defaults import RECOMMENDED
        kinds = ("h", "p", "m", "b", "w")
        for effort in ("", "xhigh"):
            with self.subTest(effort=effort):
                rows = {row.key: row for row in model_settings(self.MENU, "claude:opus", kinds=kinds, revision=1, effort=effort)}
                self.assertEqual([value for value, _ in rows["model"].options], ["claude:opus"])
                self.assertEqual([text for _, text in rows["effort"].options], ["max", "xhigh", "high", "low"])
                self.assertEqual(rows["effort"].current, "1:xhigh")
        for effort in (RECOMMENDED, "default"):
            with self.subTest(effort=effort):
                rows = {row.key: row for row in model_settings(self.MENU, "claude:opus", kinds=kinds, revision=1, effort=effort)}
                self.assertEqual([value for value, _ in rows["effort"].options][:4], ["1:max", "1:xhigh", "1:high", "1:low"])
                self.assertEqual(rows["effort"].current, f"1:{effort}")
        kept = {row.key: row for row in model_settings(self.MENU, "claude:default", kinds=kinds, revision=1, effort="")}
        self.assertEqual([value for value, _ in kept["model"].options], ["claude:default", "claude:opus"])

    def test_a_launch_never_starts_from_the_nameless_model(self):
        from covey.app.preselection import launch_selection, switch_selection
        from covey.core.defaults import RECOMMENDED
        saved = {"model_choice": "claude:default", "effort": RECOMMENDED, "workdir": "/absolute/w"}
        found = launch_selection(self.MENU, [], model="claude:default", work_mode="", fresh=True, saved=saved)
        self.assertEqual((found.model, found.effort), ("claude:opus", "xhigh"))
        picked = launch_selection(self.MENU, [], model="claude:opus", work_mode="", fresh=False,
                                  draft={"model": "claude:default", "effort": "default"})
        self.assertEqual((picked.model, picked.effort), ("claude:default", "default"))  # Tapped on a card sent before.
        usual = [{"model": "claude:opus", "effort": RECOMMENDED, "at": 1}]
        topic = {"harness_id": "claude", "model": "opus", "effort": "default"}
        self.assertEqual(switch_selection(self.MENU, usual, topic, ["w"]).effort, "default")  # Its binding, as it is.
        other = {"harness_id": "claude", "model": "gone", "effort": "default"}
        self.assertEqual(switch_selection({**self.MENU, "claude:gone": {"harness": "claude", "levels": ["high", "xhigh", "max"]}},
                                          [], other, ["w"]).effort, "default")

    def test_the_web_pages_new_topic_names_its_budgets(self):
        from covey.app.web_launch import catalog
        from covey.core.state import write_private_text
        write_private_text(self.ctx.state_dir / "launch-models" / "claude.json", json.dumps({
            "updated_at": time.time(), "models": {
                "default": {"display_name": "Default (recommended)", "levels": ["low", "high", "max"], "default": "high"},
                "opus": {"display_name": "Opus 5.5", "levels": ["low", "high", "xhigh", "max"], "default": "xhigh"}}}))
        found = catalog(SimpleNamespace(topics=self.app.topics, runtime=self.app.runtime), self.ctx, "oc_owner")
        budgets = [budget["text"] for model in found["models"] for budget in model["budgets"]]
        self.assertTrue(budgets)
        self.assertNotIn("Default (recommended)", [model["label"] for model in found["models"]], "网页开新话题的模型（U5）")
        self.assertIn("Opus 5.5", [model["label"] for model in found["models"]])
        self.assertNotRegex(json.dumps(found["preselect"]), "top_minus_one|claude:default")
        self.assertEqual([text for text in budgets if NAMELESS.search(text)], [], "网页开新话题的思考预算（U5）")
        values = {budget["value"] for model in found["models"] for budget in model["budgets"]}
        self.assertTrue(all(model["start_budget"] in values for model in found["models"] if model["budgets"]))


# A button's words (docs/ui.md 6): no emoji; none of 替换掉的写法 (read from the table, those said with a condition aside).
EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿←-⇿⏩-⏺️]")


def retired_buttons() -> set:
    text = UI_DOC.read_text(encoding="utf-8")
    table = text.split("## 6. 按钮", 1)[1].split("\n规则", 1)[0]
    found = set()
    for line in table.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 3 or cells[0] in ("操作", "---"):
            continue
        for word in cells[2].split("、"):
            word = word.strip()
            if word and word != "—" and "（" not in word:
                found.add(word)
    return found


class ButtonWordsTest(unittest.TestCase):
    """U6: every card's buttons say what docs/ui.md 6 says."""

    def test_the_retired_words_are_read(self):
        self.assertLessEqual({"🔁 换配置", "↩️ 回退", "⏹ 停止", "复制诊断摘要", "算了"}, retired_buttons())

    def test_no_emoji_and_no_retired_words(self):
        retired = retired_buttons()
        for key, value in CARDS.items():
            for button in buttons(value):
                label = words(button)
                with self.subTest(key=key, button=label):
                    self.assertIsNone(EMOJI.search(label), f"{key} 的按钮“{label}”带 emoji（U6）")
                    self.assertNotIn(label, retired, f"{key} 的按钮“{label}”是替换掉的写法（U6）")


class TidyFailedTest(unittest.TestCase):
    """U13: a 整理 that failed says through which Harness, why and what next, its buttons by the reason; 过一会儿再试 only for
    what passes by itself."""

    def test_the_failed_cards_say_who_why_and_next(self):
        from covey.cards.project_proposal import OPEN_SETTINGS_KIND
        want = {"tidy.tidy_failed_login": (("换辅助模型", "看诊断"), "Codex", False),
                "tidy.tidy_failed_refused": (("换辅助模型",), "Claude Code", False),
                "tidy.tidy_failed_timeout": ((), "Codex", True),
                "tidy.tidy_failed_none": (("换辅助模型",), "", False)}
        for key, (labels, harness, retry) in want.items():
            with self.subTest(key=key):
                card = CARDS[key]
                text = "\n".join(shown_texts(card))
                self.assertIn("这次没整理成：" + harness, text)
                self.assertEqual("过一会儿再试" in text, retry)
                found = [words(button) for button in buttons(card)]
                self.assertEqual(tuple(found), labels)
                self.assertTrue(all(kind(button) == OPEN_SETTINGS_KIND for button in buttons(card)))

    def test_a_card_kept_before_the_reason_codes_promises_no_retry(self):
        self.assertNotIn("过一会儿再试", "\n".join(shown_texts(CARDS["tidy.tidy_failed"])))


class AssistSectionTest(unittest.TestCase):
    """U14: 辅助模型 is a section of 配置 of its own, folded like the others, its row and its line when open, there with the
    butler off too."""

    def test_open_closed_and_without_the_butler(self):
        from covey.cards.butler_panel import ASSIST, ASSIST_NOTE, SECTION
        opened = "\n".join(shown_texts(CARDS["settings.config_assist"]))
        self.assertIn("**辅助模型**", opened)
        self.assertIn(ASSIST_NOTE, opened)
        self.assertIn("现在：", opened)
        values = [node["value"] for node in walk(CARDS["settings.config_assist"]) if isinstance(node.get("value"), dict)]
        self.assertTrue(any(value.get("kind") == ASSIST for value in values))
        closed = [node["value"] for node in walk(CARDS["settings.config_assist_closed"]) if isinstance(node.get("value"), dict)]
        self.assertTrue(any(value.get("kind") == SECTION and value.get("open") == "assist" for value in closed))


class MoreFoldTest(unittest.TestCase):
    """U15: the rows nobody changes are in their open section's 更多 at its end, closed unless the click was in it; 脱敏 on
    and 每条直接开一个话题 stay in sight."""

    def test_closed_open_and_in_sight(self):
        from covey.cards.butler_panel import FOLDED, MODE, PROVIDER, REDACTION

        def mores(card):
            return [node for node in walk(card) if node.get("tag") == "collapsible_panel"
                    and str(node.get("element_id", "")).startswith("more_")]
        closed, = mores(CARDS["settings.config_butler"])
        opened, = mores(CARDS["settings.config_butler_more"])
        self.assertEqual((closed["expanded"], opened["expanded"]), (False, True))
        self.assertTrue(closed["header"]["title"]["content"].startswith("更多："))
        inside = {node["value"]["kind"] for node in walk(opened) if isinstance(node.get("value"), dict)}
        self.assertTrue({PROVIDER, REDACTION, MODE} <= inside)
        self.assertEqual(len(FOLDED), 8)  # 10 till 独立复核's 模型 and 供应商 went with its section (T937).
        # 纯话题: the mode is no usual choice and is the section's one row, in sight, no 更多.
        self.assertEqual(mores(CARDS["settings.config_topics"]), [])


# The rules of this module, by their number in docs/ui.md 13.
RULES = {"U1": "BandTest", "U2": "HeaderTest", "U3": "SpeakerRowTest", "U4": "QuestionButtonsTest", "U5": "ModelChoiceTest",
         "U6": "ButtonWordsTest", "U11": "BoardBandTest", "U13": "TidyFailedTest", "U14": "AssistSectionTest",
         "U15": "MoreFoldTest"}


def decisions() -> list:
    """(number, the 谁核对 cell) of each row of docs/ui.md 13's table."""
    section = UI_DOC.read_text(encoding="utf-8").split("## 13. 拍板记录", 1)[1]
    rows = []
    for line in section.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if line.startswith("| U") and len(cells) == 5:
            rows.append((cells[0], cells[4]))
    return rows


class DecisionTableTest(unittest.TestCase):
    """docs/ui.md 13 and the tests that hold it say the same."""

    def test_numbers_run_on(self):
        numbers = [number for number, _ in decisions()]
        self.assertEqual(numbers, [f"U{n}" for n in range(1, len(numbers) + 1)])

    def test_each_rule_names_tests_that_are_there(self):
        for number, cell in decisions():
            named = re.findall(r"`(test_\w+)\.(\w+)`", cell)
            with self.subTest(rule=number):
                self.assertTrue(named, f"{number} 没写谁核对")
            for module, name in named:
                with self.subTest(rule=number, test=f"{module}.{name}"):
                    source = (REPO / "tests" / f"{module}.py").read_text(encoding="utf-8")
                    self.assertRegex(source, rf"(?m)^class {name}\b", f"{number} 写的 {module}.{name} 不存在")

    def test_this_module_s_rules_are_the_table_s(self):
        here = {number: re.findall(r"`test_ui_rules\.(\w+)`", cell) for number, cell in decisions()}
        self.assertEqual({number: names[0] for number, names in here.items() if names}, RULES)
        for number, name in RULES.items():
            with self.subTest(rule=number):
                self.assertIn(number, (globals()[name].__doc__ or "").split(":")[0])


if __name__ == "__main__":
    unittest.main()
