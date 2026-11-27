"""G3 (WAVE12 C11, W2-D 包 C): every card write is checked before it goes (core/card_limits.py ``check``, feishu/messenger.py
``refused``), a card over what Feishu takes is never sent and fails as Feishu would have answered, a card that could not be
updated says so under itself once (``patch_or_tell``), and the cards themselves keep within the limits at their largest."""
from __future__ import annotations

import json
import lzma
import unittest
from unittest import mock

from support import REPO

from covey.cards import project_proposal as tidy_cards
from covey.core import card_limits
from covey.feishu import messenger as messenger_module, transport
from covey.feishu.messenger import Messenger, Voice
from covey.feishu.transport import NOT_SENT, SENT, UNCERTAIN, Failure

VOICE = Voice("t", "COVEY_TEST")
GOLDEN = REPO / "tests" / "golden" / "cards.json.xz"


def card_of(nodes, text="x"):
    """A card of exactly ``nodes`` tagged nodes (each a markdown element)."""
    return {"schema": "2.0", "body": {"elements": [{"tag": "markdown", "content": text} for _ in range(nodes)]}}


def card_of_bytes(size):
    """A card of one markdown element whose JSON is exactly ``size`` bytes."""
    built = card_of(1, "")
    built["body"]["elements"][0]["content"] = "a" * (size - card_limits.json_size(built))
    assert card_limits.json_size(built) == size
    return built


class CheckTest(unittest.TestCase):
    def test_the_hard_limits_are_feishus_and_only_a_card_message_is_bounded_by_its_bytes(self):
        self.assertIsNone(card_limits.check(card_of(200)))
        over = card_limits.check(card_of(201))
        self.assertEqual((over.code, over.nodes, over.limit), (card_limits.NODES_OVER, 201, 200))
        self.assertIsNone(card_limits.check(card_of_bytes(30_000), message=True))
        over = card_limits.check(card_of_bytes(30_001), message=True)
        self.assertEqual((over.code, over.bytes, over.limit), (card_limits.SIZE_OVER, 30_001, 30_000))
        # A CardKit card is refused by the size it renders at, not by its bytes (a task card's body is up to 28,000
        # characters): its bytes are never refused here.
        self.assertIsNone(card_limits.check(card_of_bytes(90_000)))
        self.assertEqual(card_limits.check(card_of(196), nodes=card_limits.TARGET_NODES).code, card_limits.NODES_OVER)
        self.assertIsNone(card_limits.check(object()))  # Nothing it can measure: never raises, goes as before.

    def test_the_targets_are_only_named(self):
        self.assertEqual(card_limits.over_target(card_of(195)), "")
        self.assertEqual(card_limits.over_target(card_of(196)), "nodes=196")
        self.assertEqual(card_limits.over_target(card_of_bytes(29_001), message=True), "bytes=29001")
        self.assertEqual(card_limits.over_target(card_of_bytes(29_001)), "")
        heavy = card_of(150, "1\n" * 20)
        self.assertGreater(card_limits.card_cost(heavy), card_limits.CARD_COST_BUDGET)
        self.assertTrue(card_limits.over_target(heavy).startswith("cost="))
        self.assertEqual(card_limits.over_target(heavy, cost=False), "")

    def test_the_cards_modules_name_the_same_measure(self):
        from covey.cards import card_cost, elements, pages, question_cards
        from covey.core import ui_samples
        self.assertIs(elements.element_count, card_limits.element_count)
        self.assertIs(elements.json_size, card_limits.json_size)
        self.assertEqual((elements.CARD_NODES, elements.CARD_BYTES), (200, 30_000))
        self.assertIs(card_cost.card_cost, card_limits.card_cost)
        self.assertIs(card_cost.text_cost, card_limits.text_cost)
        self.assertEqual((card_cost.CARD_COST_BUDGET, card_cost.CARD_TOO_BIG), (300_000, "200860"))
        self.assertIs(pages.sent_size, card_limits.sent_size)
        self.assertEqual((question_cards.CARD_NODES, question_cards.CARD_BYTES), (195, 29_000))
        self.assertEqual(ui_samples.nodes(card_of(7)), 7)


class SamplesWithinTest(unittest.TestCase):
    """The cards at their largest keep within Feishu's limits: every sample tests/test_card_golden.py draws (the crowded
    board, the full digests, the task card over its budget among them) and the 整理专项 card of a 整理 that did a few
    hundred things."""

    def test_every_golden_card_passes_the_check_and_keeps_to_the_node_target(self):
        cards = json.loads(lzma.open(GOLDEN).read())
        self.assertGreater(len(cards), 400)
        for name, built in cards.items():
            with self.subTest(name):
                self.assertIsNone(card_limits.check(built))
                self.assertLessEqual(card_limits.element_count(built), card_limits.TARGET_NODES)
                if name.startswith(("trace.", "task.")):  # Task cards are CardKit's: bounded by their rendered size.
                    self.assertLessEqual(card_limits.card_cost(built), card_limits.CARD_COST_BUDGET)
                else:
                    self.assertIsNone(card_limits.check(built, message=True, size=card_limits.TARGET_BYTES))

    def test_a_tidy_card_of_two_hundred_of_everything_keeps_within_the_targets(self):
        many = 200
        view = {"run": "r", "state": "done", "scope": "Covey",
                "did": [f"把 T{n}「一个名字不短的话题标题第 {n} 号」归进专项「某个专项」" for n in range(many)],
                "proposed": [f"新专项名字第 {n} 号" for n in range(many)],
                "pending": [f"还在等你定的专项第 {n} 号" for n in range(many)],
                "skipped": [f"T{n} 跳过了：它还在跑，下次整理再看" for n in range(many)],
                "rows": [{"proposal": f"p{n}", "line": f"把 T{n} 移出专项「某个专项」", "state": "open"} for n in range(many)]}
        built = tidy_cards.tidy_card(view)
        self.assertIsNone(card_limits.check(built, message=True))
        self.assertIsNone(card_limits.check(built, message=True, nodes=card_limits.TARGET_NODES,
                                            size=card_limits.TARGET_BYTES))
        text = json.dumps(built, ensure_ascii=False)
        self.assertIn(f"另有 {many - tidy_cards.DID_LINES} 条没列出", text)
        self.assertIn(f"另有 {many - tidy_cards.SKIPPED_LINES} 条没列出", text)
        self.assertIn(f" 等 {many} 个", text)
        self.assertIn("新专项的提议（200 个）", text)
        self.assertIn("还在等你定（200 个）", text)
        one = "超" * (tidy_cards.LINE_CHARS + 50)
        line = json.dumps(tidy_cards.tidy_card({"run": "r", "state": "done", "did": [one]}), ensure_ascii=False)
        self.assertNotIn(one, line)
        self.assertIn("超" * (tidy_cards.LINE_CHARS - 1) + "…", line)

    def test_a_small_tidy_card_is_drawn_as_before(self):
        view = {"run": "r", "state": "done", "did": ["把 T1 归进「A」", "把 T2 归进「A」"], "proposed": ["B"],
                "pending": ["C"], "skipped": ["T3 跳过了"]}
        text = json.dumps(tidy_cards.tidy_card(view), ensure_ascii=False)
        self.assertNotIn("另有", text)
        self.assertNotIn(" 等 ", text)
        for words in ("· 把 T1 归进「A」", "「B」", "「C」", "T3 跳过了"):
            self.assertIn(words, text)


class _Lark:
    """Feishu, as the Messenger reaches it: every call kept; ``answer`` what it answers, ``failure`` the failure it leaves."""

    def __init__(self):
        self.calls, self.sent, self.answer, self.failure = [], [], {}, None

    def call(self, argv, **kwargs):
        self.calls.append(argv)
        transport.set_failure(self.failure)
        return self.answer

    def cardkit(self, profile, method, path, data, **kwargs):
        self.calls.append((method, path))
        transport.set_failure(self.failure)
        return self.answer

    def send(self, **kwargs):
        self.sent.append(kwargs)
        return True


class _MessengerCase(unittest.TestCase):
    def setUp(self):
        self.logs = []
        self.messenger = Messenger(self.logs.append)
        self.lark = _Lark()
        transport.set_failure(None)
        self.addCleanup(transport.set_failure, None)
        for target, value in (("covey.feishu.messenger.call", self.lark.call),
                              ("covey.feishu.messenger.send_message_with_fallback", self.lark.send),
                              ("covey.feishu.cardkit.cardkit_api", self.lark.cardkit)):
            patcher = mock.patch(target, side_effect=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def writes(self, built):
        """Each card write of ``built``, as (step, what came back)."""
        m = self.messenger
        return {"patch": m.patch_card("p", "om_1", built), "create": m.create_card("p", built),
                "put": m.put_card("p", "c1", built, sequence=2, key="k"),
                "write": m.write_card("p", "POST", "c1", "elements", {"elements": built["body"]["elements"]},
                                      sequence=3, key="k")}


class MessengerCheckTest(_MessengerCase):
    def test_a_card_of_201_nodes_is_never_sent_and_fails_as_feishu_would_answer(self):
        for step, came in self.writes(card_of(201)).items():
            with self.subTest(step):
                self.assertFalse(came)
        self.assertEqual(self.lark.calls, [])
        failure = transport.last_failure()
        self.assertEqual((failure.code, failure.category, failure.ambiguous), ("300305", "api_rejected", False))
        self.assertEqual(transport.reason(failure), "卡片超过飞书一张卡 200 个组件的上限（300305）")
        self.assertEqual(sum("card refused before sending" in line for line in self.logs), 4)
        self.messenger.patch_card("p", "om_1", card_of(201))
        self.assertEqual(self.messenger.last_outcome(), NOT_SENT)

    def test_a_card_message_of_30001_bytes_is_never_sent_a_cardkit_card_of_it_goes(self):
        self.assertFalse(self.messenger.patch_card("p", "om_1", card_of_bytes(30_001)))
        self.assertEqual(self.lark.calls, [])
        self.assertEqual(transport.last_failure().code, "200860")
        self.assertEqual(self.messenger.last_outcome(), NOT_SENT)
        self.assertEqual(self.messenger.put_card("p", "c1", card_of_bytes(30_001), sequence=2, key="k"), {})
        self.assertEqual(self.lark.calls, [("PUT", "/open-apis/cardkit/v1/cards/c1")])

    def test_cards_at_the_limits_go_and_one_over_a_target_is_logged_once_a_minute(self):
        came = self.writes(card_of(200))
        self.assertTrue(came["patch"])
        self.assertEqual([came[step] for step in ("create", "put", "write")], [{}, {}, {}])
        self.assertEqual(len(self.lark.calls), 4)
        self.assertTrue(self.messenger.patch_card("p", "om_1", card_of_bytes(30_000)))
        self.assertEqual(self.messenger.last_outcome(), SENT)
        near = [line for line in self.logs if "card over its target" in line]
        self.assertEqual(near, ["card over its target step=patch nodes=200", "card over its target step=create nodes=200",
                                "card over its target step=put nodes=200", "card over its target step=write nodes=200"])
        self.messenger.patch_card("p", "om_1", card_of(199))
        self.assertEqual(len([line for line in self.logs if "card over its target" in line]), 4)

    def test_a_card_that_cannot_be_measured_goes_as_before(self):
        with mock.patch.object(card_limits, "element_count", side_effect=RecursionError):
            self.assertTrue(self.messenger.patch_card("p", "om_1", card_of(300)))
        self.assertEqual(len(self.lark.calls), 1)


class PatchOrTellTest(_MessengerCase):
    def test_a_card_that_did_not_take_says_so_under_itself_once(self):
        self.lark.answer, self.lark.failure = None, Failure("api_rejected", code="230099", detail="card message too old")
        self.assertFalse(self.messenger.patch_or_tell("p", "om_card", card_of(3), voice=VOICE, chat_id="oc_1",
                                                      what="「整理专项」卡"))
        self.assertEqual(len(self.lark.sent), 1)
        line = self.lark.sent[0]
        self.assertEqual((line["reply_to_message_id"], line["chat_id"]), ("om_card", "oc_1"))
        self.assertEqual(line["idempotency_key"], VOICE.safe_key(VOICE.key("card-not-updated", "om_card")))
        self.assertEqual(line["text"], "⚠️ 这张「整理专项」卡没更新：飞书拒绝（230099：card message too old）。"
                                       "卡上的状态可能是旧的，稍后再点一次或直接在话题里说。")
        self.assertEqual(self.messenger.last_outcome(), NOT_SENT)  # The card's, not the line's.
        self.assertEqual(transport.last_failure().code, "230099")
        self.assertFalse(self.messenger.patch_or_tell("p", "om_card", card_of(3), voice=VOICE, what="「整理专项」卡"))
        self.assertEqual(len(self.lark.sent), 1)  # The same card: said once.
        self.assertFalse(self.messenger.patch_or_tell("p", "om_other", card_of(3), voice=VOICE))
        self.assertEqual(len(self.lark.sent), 2)
        self.assertTrue(any("card not updated what=「整理专项」卡 outcome=not_sent" in line for line in self.logs))

    def test_one_over_the_limit_and_one_that_may_have_taken_say_why(self):
        self.messenger.patch_or_tell("p", "om_big", card_of(201), voice=VOICE, where="到网页工作台看")
        self.assertEqual(self.lark.sent[0]["text"],
                         "⚠️ 这张卡片没更新：卡片超过飞书一张卡 200 个组件的上限（300305）。到网页工作台看。")
        self.assertEqual(self.lark.calls, [])  # The card itself never went.
        self.lark.answer, self.lark.failure = None, Failure("network_timeout", True, True)
        self.messenger.patch_or_tell("p", "om_slow", card_of(3), voice=VOICE)
        self.assertEqual(self.lark.sent[1]["text"],
                         "⚠️ 这张卡片可能没更新：飞书超时没有回应。卡上的状态可能是旧的，稍后再点一次或直接在话题里说。")
        self.assertEqual(self.messenger.last_outcome(), UNCERTAIN)

    def test_a_card_that_took_or_no_voice_says_nothing_and_the_line_never_recurses(self):
        self.assertTrue(self.messenger.patch_or_tell("p", "om_ok", card_of(3), voice=VOICE))
        self.lark.answer = None
        self.assertFalse(self.messenger.patch_or_tell("p", "om_quiet", card_of(3), voice=None))
        self.assertEqual(self.lark.sent, [])
        self.assertTrue(any("card not updated" in line for line in self.logs))

        def again(**kwargs):  # The line's own send asking for another card update: no second line, no loop.
            self.lark.sent.append(kwargs)
            self.messenger.patch_or_tell("p", "om_inner", card_of(3), voice=VOICE)
            raise OSError("the line did not go")

        with mock.patch.object(messenger_module, "send_message_with_fallback", side_effect=again):
            self.assertFalse(self.messenger.patch_or_tell("p", "om_outer", card_of(3), voice=VOICE))
        self.assertEqual([sent["reply_to_message_id"] for sent in self.lark.sent], ["om_outer"])
        self.assertTrue(any("card not updated line not sent (OSError)" in line for line in self.logs))
        self.assertFalse(self.messenger.patch_or_tell("p", "om_outer", card_of(3), voice=VOICE))
        self.assertEqual(len(self.lark.sent), 1)


if __name__ == "__main__":
    unittest.main()
