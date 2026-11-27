"""样张 (docs/ui.md 12 改外观先选样张): a checkpoint question's options carrying samples of a card, kept inert when the
question is registered (covey/core/ui_samples.py), drawn above the question on its own card (cards/question_cards.py),
never answered by a project's lead (core/project_gates.py), and the tool that makes and checks them (scripts/ui_samples.py)."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import REPO  # noqa: E402
from support.world import QuestionFixture  # noqa: E402

from covey.core import ui_samples  # noqa: E402


def button(label, kind="bridge_question", look="default"):
    return {"tag": "button", "element_id": "send", "name": "submit", "text": {"tag": "plain_text", "content": label},
            "type": look, "form_action_type": "submit", "confirm": {"title": {"tag": "plain_text", "content": "确定"}},
            "behaviors": [{"type": "callback", "value": {"kind": kind}}]}


def form_card():
    """A card with everything a click could do: a form with a required field and a submit button, a button whose callback
    is its ``value``, a clickable container, a speaker's picture with a test's key, a title bar and colours."""
    return {"schema": "2.0", "header": {"template": "default", "title": {"tag": "plain_text", "content": "换配置"},
                                        "subtitle": {"tag": "plain_text", "content": "话题 T70"}},
            "config": {"style": {"color": {"topic-2": {"light_mode": "rgba(1,1,1,1)", "dark_mode": "rgba(1,1,1,1)"}}}},
            "body": {"elements": [
                {"tag": "form", "name": "switch_form", "element_id": "f1", "elements": [
                    {"tag": "input", "name": "note", "required": True, "placeholder": {"tag": "plain_text", "content": "写"}},
                    button("确认更换")]},
                {"tag": "button", "text": {"tag": "plain_text", "content": "检查并继续"}, "value": {"kind": "bridge_job_retry"}},
                {"tag": "interactive_container", "_ask": {"who": "管家"}, "elements": [{"tag": "markdown", "content": "进话题"}],
                 "behaviors": [{"type": "open_url", "default_url": "https://example.invalid/t70"}]},
                {"tag": "img", "img_key": "img_v3_fake_butler"},
                {"tag": "select_static", "name": "pick", "options": [{"text": {"tag": "plain_text", "content": "甲"},
                                                                       "value": "0"}]}]}}


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


class InertTest(unittest.TestCase):
    def test_a_sample_can_do_nothing(self):
        kept = ui_samples.sample(form_card(), 0)
        drawn = kept["elements"]
        text = json.dumps(drawn, ensure_ascii=False)
        for key in ("behaviors", "confirm", "form_action_type", "required", "_ask"):
            self.assertNotIn(f'"{key}"', text)
        self.assertNotIn('"tag": "form"', text)
        self.assertNotIn("bridge_job_retry", text)
        self.assertIn('"value": "0"', text)  # An option's value is no callback.
        self.assertIn(ui_samples.IMAGE_MARK, text)
        self.assertEqual(drawn[0]["content"], "**换配置**\n<font color='grey'>话题 T70</font>")
        self.assertEqual(kept["colors"], {"topic-2": {"light_mode": "rgba(1,1,1,1)", "dark_mode": "rgba(1,1,1,1)"}})

    def test_two_samples_never_share_an_id_or_a_name(self):
        ids = []
        for index in range(3):
            ids += [node[key] for node in walk(ui_samples.sample(form_card(), index)["elements"])
                    for key in ("element_id", "name") if key in node]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(len(name) <= 20 and name[0].isalpha() for name in ids))

    def test_what_a_file_may_hold(self):
        element = {"tag": "markdown", "content": "一行"}
        for data in ({"schema": "2.0", "body": {"elements": [element]}}, {"elements": [element]}, [element]):
            with self.subTest(data=data):
                self.assertEqual(ui_samples.sample(data, 0), {"elements": [element]})
        for data in ({}, [], {"body": {}}, "text", [{"content": "没有 tag"}], {"elements": "x"}):
            with self.subTest(data=data):
                self.assertEqual(ui_samples.sample(data, 0), {"note": ui_samples.NOT_CARD})
        many = [element] * (ui_samples.SAMPLE_NODES + 1)
        self.assertTrue(ui_samples.sample(many, 0)["note"].startswith(ui_samples.TOO_BIG))

    def test_reading_never_raises(self):
        with tempfile.TemporaryDirectory() as work:
            good, broken, large = (Path(work) / name for name in ("a.json", "b.json", "c.json"))
            good.write_text(json.dumps([{"tag": "markdown", "content": "a"}]))
            broken.write_text("{")
            large.write_text(" " * (ui_samples.FILE_BYTES + 1))
            self.assertIn("elements", ui_samples.read(str(good), 0))
            self.assertEqual(ui_samples.read(str(broken), 0), {"note": ui_samples.NOT_READ})
            self.assertEqual(ui_samples.read(str(Path(work) / "gone.json"), 0), {"note": ui_samples.NOT_READ})
            self.assertEqual(ui_samples.read("a.json", 0), {"note": ui_samples.NOT_ABSOLUTE})
            self.assertEqual(ui_samples.read(None, 0), {"note": ui_samples.NOT_ABSOLUTE})
            self.assertTrue(ui_samples.read(str(large), 0)["note"].startswith(ui_samples.TOO_BIG))


class CarryTest(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.files = []
        for n in range(6):
            path = Path(self.work.name) / f"{n}.json"
            path.write_text(json.dumps([{"tag": "markdown", "content": f"样张 {n}"}]))
            self.files.append(str(path))

    def question(self, count=3, **fields):
        return {"id": "look", "question": "用哪一版？", "options": [
            {"label": chr(65 + n), "sample": self.files[n]} for n in range(count)], **fields}

    def test_the_files_are_read_once_and_kept(self):
        plain = {"id": "other", "question": "还有呢？"}
        carried = ui_samples.carry([self.question(), plain])
        self.assertIs(carried[1], plain)
        options = carried[0]["options"]
        self.assertEqual([ui_samples.kept(o)["elements"][0]["content"] for o in options], ["样张 0", "样张 1", "样张 2"])
        Path(self.files[0]).unlink()
        self.assertEqual(ui_samples.carry(carried), carried)  # Kept: the file is not read again.

    def test_at_most_four_and_only_on_a_single_choice(self):
        options = ui_samples.carry([self.question(6)])[0]["options"]
        self.assertEqual([bool(ui_samples.kept(o)) for o in options], [True] * ui_samples.MOST + [False] * 2)
        self.assertNotIn("sample", options[-1])
        for multiple in ({"multiple": True}, {"multiSelect": True}):
            with self.subTest(multiple=multiple):
                options = ui_samples.carry([self.question(**multiple)])[0]["options"]
                self.assertFalse(any("sample" in o for o in options))

    def test_a_device_topic_says_why(self):
        options = ui_samples.carry([self.question(1)], device=True)[0]["options"]
        self.assertEqual(ui_samples.kept(options[0]), {"note": ui_samples.ON_DEVICE})


def record(questions, **fields):
    return {"token": "0123456789abcdef", "status": "pending", "topic_id": "a" * 64, "chat_id": "oc_x", "job_id": 5,
            "blocking": True, "questions": questions, "created_at": 0, **fields}


def picked(label):
    return {"label": label, "description": f"{label} 的说明",
            "sample": ui_samples.sample([{"tag": "markdown", "content": f"{label} 的样子"}], ord(label[0]) - 65)}


class SampleCardTest(unittest.TestCase):
    def test_the_samples_come_before_the_question_and_its_buttons(self):
        from covey.cards.question_cards import SAMPLES_HEAD, question_card
        card = question_card(record([{"id": "look", "question": "用哪一版？", "options": [picked("A"), picked("B")]}]),
                             "T70", "问题卡")
        elements = card["body"]["elements"]
        contents = [e.get("content", "") for e in elements]
        self.assertIn(SAMPLES_HEAD.format(n=2), contents[1])
        self.assertEqual(contents[3:5], ["**A**\n<font color='grey'>A 的说明</font>", "A 的样子"])
        self.assertEqual(contents[6:8], ["**B**\n<font color='grey'>B 的说明</font>", "B 的样子"])
        self.assertEqual(elements[-1]["tag"], "form")
        form = json.dumps(elements[-1], ensure_ascii=False)
        self.assertNotIn("A 的说明", form)  # Said over its sample, not again in the form.
        self.assertIn('"content": "A"', form)  # A button per option.

    def test_answered_the_samples_are_gone(self):
        from covey.cards.question_cards import question_card
        card = question_card(record([{"id": "look", "question": "用哪一版？", "options": [picked("A"), picked("B")]}],
                                    status="answered", answers={"look": {"answers": ["A"]}}), "T70", "问题卡")
        self.assertNotIn("A 的样子", json.dumps(card, ensure_ascii=False))

    def test_a_card_too_large_leaves_out_its_last_samples(self):
        from covey.cards.question_cards import CARD_BYTES, CARD_NODES, SAMPLE_LEFT_OUT, question_card
        bulky = [{"tag": "markdown", "content": "字" * 130} for _ in range(35)]  # 14 KB: two do not fit with the form.
        options = [{"label": label, "sample": ui_samples.sample(bulky, n)} for n, label in enumerate("ABC")]
        options.append({"label": "D", "sample": {"note": ui_samples.NOT_READ}})
        card = question_card(record([{"id": "look", "question": "用哪一版？", "options": options}]), "T70", "问题卡")
        text = json.dumps(card, ensure_ascii=False)
        self.assertLessEqual(ui_samples.size(card), CARD_BYTES)
        self.assertLessEqual(ui_samples.nodes(card), CARD_NODES)
        self.assertEqual(text.count(SAMPLE_LEFT_OUT), 2)  # C, then B; A stays, D says its own note.
        self.assertIn(ui_samples.NOT_READ, text)
        self.assertLess(text.index("**A**"), text.index("字"))

    def test_the_samples_colours_join_the_card(self):
        from covey.cards.question_cards import question_card
        colored = {"label": "A", "sample": {"elements": [{"tag": "markdown", "content": "a"}],
                                            "colors": {"mine": {"light_mode": "red", "dark_mode": "red"}}}}
        card = question_card(record([{"id": "look", "question": "用哪一版？", "options": [colored, picked("B")]}]), "T70")
        self.assertEqual(card["config"]["style"]["color"]["mine"], {"light_mode": "red", "dark_mode": "red"})

    def test_the_top_level_copy_has_them_too(self):
        from covey.cards.question_cards import mirror_card
        card = mirror_card(record([{"id": "look", "question": "用哪一版？", "options": [picked("A"), picked("B")]}]),
                           "T70", "这一轮的输出")
        text = json.dumps(card, ensure_ascii=False)
        self.assertLess(text.index("这一轮的输出"), text.index("A 的样子"))


def sized(label, index, nodes):
    """An option whose sample is ``nodes`` Markdown elements (one node each)."""
    return {"label": label, "description": f"{label} 的说明",
            "sample": ui_samples.sample([{"tag": "markdown", "content": f"{label} 第 {n} 行"} for n in range(nodes)], index)}


def t884(nodes, **fields):
    """The shape of T884's #7904 (2026-11-27): four questions of 3, 3, 4 and 3 options, six of them with a sample."""
    def question(n, labels, sampled):
        return {"id": f"q{n}", "header": f"第{n}项", "question": f"第 {n} 题用哪一版？",
                "options": [sized(label, n * 10 + j, nodes) if j < sampled else {"label": label, "description": "不画"}
                            for j, label in enumerate(labels)]}
    return record([question(1, "ABC", 2), question(2, "ABC", 2), question(3, "ABCD", 1), question(4, "ABC", 1)], **fields)


def uploading_files():
    """An answer card's 产物 panel while its upload runs (7 nodes, as the copy's was when CardKit refused it)."""
    from covey.cards.artifact_panels import drive_artifact_panel
    return drive_artifact_panel(["/absolute/a.png", "/absolute/b.png"], {"job_id": 5}, "a" * 64, {},
                                element_id="answer_artifacts", fold_uploading=True)


class CardBudgetTest(unittest.TestCase):
    """CardKit refuses a card over 200 tagged nodes (300305): T884's top-level copy was 201 with its row and 产物
    uploading, the samples having been counted against the card in the topic only (2026-11-27, T889)."""

    def assert_within(self, card):
        from covey.cards.question_cards import CARD_BYTES, CARD_NODES, SUBMIT_LABEL
        self.assertLessEqual(ui_samples.nodes(card), CARD_NODES)
        self.assertLessEqual(ui_samples.size(card), CARD_BYTES)
        self.assertIn(f'"content": "{SUBMIT_LABEL}"', json.dumps(card, ensure_ascii=False))  # Still answerable.

    ask = staticmethod(lambda: __import__("covey.cards.answer_card", fromlist=["ask_block"]).ask_block(
        "选哪一版", at="16:54", url="https://example.invalid/t884", who="T884 辅助模型", doing="waiting"))

    def test_the_copy_counts_its_row_and_its_files(self):
        from covey.cards.question_cards import mirror_card, question_card
        for nodes in range(4, 31):
            with self.subTest(nodes=nodes):
                asked = t884(nodes)
                self.assert_within(question_card(asked, "T884", "辅助模型"))
                self.assert_within(mirror_card(asked, "T884", "", ask=self.ask(), files=uploading_files()))

    def test_a_sample_left_out_of_the_copy_says_the_card_in_the_topic_has_it(self):
        from covey.cards.question_cards import SAMPLE_IN_TOPIC, mirror_card
        copies = [json.dumps(mirror_card(t884(nodes), "T884", "", ask=self.ask(), files=uploading_files()),
                             ensure_ascii=False) for nodes in range(4, 31)]
        self.assertTrue(any(SAMPLE_IN_TOPIC in copy for copy in copies))

    def test_the_output_goes_apart_before_a_sample_is_left_out(self):
        from covey.cards.question_cards import SAMPLE_LEFT_OUT, mirror_fit
        from covey.core.trace import STAGE_HEADING
        asked = t884(19)  # 193 nodes with all its samples: the output and its 中途的汇报 fold do not fit beside them.
        alone = mirror_fit(asked, "T884", "", files=uploading_files())
        self.assertTrue(alone.whole)
        self.assertNotIn(SAMPLE_LEFT_OUT, json.dumps(alone.card, ensure_ascii=False))
        output = "输出\n\n---\n\n" + STAGE_HEADING + "\n\n第一段汇报。\n\n第二段汇报。"
        crowded = mirror_fit(asked, "T884", output, files=uploading_files())
        self.assertFalse(crowded.whole)  # app/question_display.py mirror then sends the output on its own first.
        self.assert_within(crowded.card)

    def test_every_sample_left_out_goes_on_one_line(self):
        from covey.cards.question_cards import SAMPLE_LEFT_OUT, SAMPLES_HEAD, SAMPLES_LEFT_OUT, question_fit
        # Twelve questions of five options, the first four with samples: even without their bodies the sample blocks
        # (a rule, a label, a note each) would pass the card.
        questions = [{"id": f"q{n}", "question": f"第 {n} 题？", "context": "这题改什么",
                      "options": [sized(chr(65 + j), n * 10 + j, 60) if n < 4 else {"label": chr(65 + j)}
                                  for j in range(5)]} for n in range(12)]
        fit = question_fit(record(questions), "T9", "很多题")
        text = json.dumps(fit.card, ensure_ascii=False)
        self.assert_within(fit.card)
        self.assertFalse(fit.whole)
        self.assertIn(SAMPLES_LEFT_OUT.format(n=20), text)
        self.assertNotIn(SAMPLE_LEFT_OUT, text)
        self.assertNotIn(SAMPLES_HEAD.format(n=20), text)
        self.assertIn("A 的说明", text)  # Each option's words, in the one line's list.

    def test_many_questions_are_drawn_smaller_then_listed(self):
        from covey.cards.question_cards import BRIEF_HEAD, BRIEF_WEB, SUPPLEMENT, question_card
        def asked(count, **fields):
            return record([{"id": f"q{n}", "header": f"第{n}项", "question": f"第 {n} 题怎么办？", "context": "背景",
                            "options": [{"label": f"选项{j}", "description": "说明"} for j in range(6)]}
                           for n in range(count)], **fields)
        smaller = question_card(asked(14), "T9", "很多题")
        self.assert_within(smaller)
        self.assertIn('"name": "option_13"', json.dumps(smaller, ensure_ascii=False))  # Every question's dropdown.
        listed = question_card(asked(40), "T9", "很多题")
        text = json.dumps(listed, ensure_ascii=False)
        self.assert_within(listed)
        self.assertIn(BRIEF_HEAD.format(n=40), text)
        self.assertIn(BRIEF_WEB, text)
        self.assertIn(f'"name": "{SUPPLEMENT}"', text)
        self.assertIn("第 39 题怎么办", text)
        with mock.patch("covey.core.butler.is_butler_topic", return_value=True):
            butler_own = question_card(asked(40), "T9", "很多题")
        self.assertNotIn(BRIEF_WEB, json.dumps(butler_own, ensure_ascii=False))  # The butler's: answered in Feishu only.

    def test_settled_and_repainted_the_card_stays_within(self):
        from covey.cards.question_cards import mirror_card, question_card
        answered = t884(30, status="answered", answers={"q1": {"answers": ["A"]}})
        self.assert_within_settled(question_card(answered, "T884", "辅助模型"))
        self.assert_within_settled(mirror_card(answered, "T884", "输出", files=uploading_files()))

    def assert_within_settled(self, card):
        from covey.cards.question_cards import CARD_NODES
        self.assertLessEqual(ui_samples.nodes(card), CARD_NODES)


class GateTest(unittest.TestCase):
    def test_a_question_with_samples_is_the_owners(self):
        from covey.core import project_gates
        question = {"id": "look", "question": "用哪一版？", "options": [picked("A"), {"label": "B"}]}
        self.assertEqual(project_gates.hit([question]), project_gates.SAMPLE)
        self.assertEqual(project_gates.hit([{"id": "x", "question": "用哪一版？", "options": [{"label": "A"}]}]), "")
        self.assertEqual(project_gates.hits(None), [])


class RegisteredTest(QuestionFixture, unittest.TestCase):
    """The question a turn leaves in its checkpoint keeps what its samples draw, and goes on a card of its own."""

    def test_registered_and_delivered_on_its_own_card(self):
        sample = Path(self.root) / "A.json"
        sample.write_text(json.dumps([{"tag": "markdown", "content": "A 的样子",
                                       "behaviors": [{"type": "callback", "value": {"kind": "bridge_job_retry"}}]}]))
        self.path.unlink()
        self.app.questions.defer_checkpoint_question(self.ctx_topic, self.job, [
            {"id": "look", "question": "用哪一版？", "options": [{"label": "A", "sample": str(sample)}, {"label": "B"}]}])
        rows = [row for row in self.ctx_topic.state_dir.joinpath("questions").glob("*.json")]
        saved = json.loads(rows[0].read_text())
        self.assertEqual(saved["questions"][0]["options"][0]["sample"], {"elements": [{"tag": "markdown",
                                                                                       "content": "A 的样子"}]})
        self.assertNotIn("sample", saved["questions"][0]["options"][1])
        self.app.questions.checkpoints.result_delivered = lambda ctx, record: True
        with mock.patch.object(self.app.questions, "publish_question_card", return_value=True) as publish:
            self.assertTrue(self.app.questions.deliver_checkpoint_questions(self.ctx_topic, self.job, True))
        self.app.cards.set_task_element.assert_not_called()
        publish.assert_called_once()


class ToolTest(unittest.TestCase):
    def run_tool(self, *argv):
        import io
        from contextlib import redirect_stdout
        sys.path.insert(0, str(REPO / "scripts"))
        import ui_samples as tool
        out = io.StringIO()
        with redirect_stdout(out):
            code = tool.main(list(argv))
        return code, out.getvalue()

    def test_golden_then_check(self):
        with tempfile.TemporaryDirectory() as work:
            a = Path(work) / "A.json"
            code, said = self.run_tool("golden", "question.card_pending", "--out", str(a))
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(a.read_text())["schema"], "2.0")
            big = Path(work) / "B.json"
            big.write_text(json.dumps([{"tag": "markdown", "content": "x"}] * (ui_samples.SAMPLE_NODES + 1)))
            code, said = self.run_tool("check", str(a), "--labels", "A 照旧（推荐）", "--out", work)
            self.assertEqual(code, 0, said)
            self.assertIn("A 照旧（推荐）：", said)
            card = json.loads((Path(work) / "question-card.json").read_text())
            self.assertEqual(card["body"]["elements"][-1]["tag"], "form")
            code, said = self.run_tool("check", str(a), str(big))
            self.assertEqual(code, 1)
            self.assertIn(ui_samples.TOO_BIG, said)
            code, said = self.run_tool("golden", "--list")
            self.assertIn("question.card_samples", said.split())


if __name__ == "__main__":
    unittest.main()
