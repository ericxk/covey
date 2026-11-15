"""The message router's rule layer (core/track_route.py): the nine rules one by one with their edges, the wordlists, the
synthetic corpus (tests/fixtures/track_route/corpus.jsonl) and the evaluation script that reads it."""
from __future__ import annotations

import ast
import importlib.util
import itertools
import sys
import unittest

from support import REPO

from covey.core import track_route
from covey.core.track_route import (ASK_JUDGE, LEGACY, MERGE, NEW, STOP, Decision, LiveJob, Message, Quote, route)

NOW = 1_800_000_000.0


def job(track, phase="running", request="整理对比表", started=300, owner=200, job_id=None):
    return LiveJob(track, job_id or f"j{track}", phase, request, NOW - started, NOW - owner)


def say(text, *live, quote=None, **fields):
    return route(Message(text, tuple(live), quote or Quote(), now=NOW, **fields))


def load_eval():
    spec = importlib.util.spec_from_file_location("track_route_eval", REPO / "scripts" / "track_route_eval.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RuleOneTest(unittest.TestCase):
    def test_no_live_task_or_the_switch_off_is_the_old_path(self):
        self.assertEqual(say("停"), Decision(LEGACY, why="1"))
        self.assertEqual(say("你好", job(1), enabled=False).action, LEGACY)
        self.assertEqual(route(Message("停", (job(1),), enabled=False)).action, LEGACY)

    def test_a_task_in_no_live_phase_is_not_live(self):
        self.assertEqual(say("你好", job(1, "done")).action, LEGACY)
        self.assertEqual(say("停", job(1, "cancelled"), job(2, "failed")).action, LEGACY)

    def test_every_live_phase_counts(self):
        for phase in sorted(track_route.LIVE_PHASES):
            self.assertNotEqual(say("帮我写首诗", job(1, phase)).action, LEGACY, phase)


class RuleTwoTest(unittest.TestCase):
    def test_every_stop_word_stops_the_newest_track(self):
        live = (job(1, started=400), job(2, started=100), job(3, started=250))
        for word in sorted(track_route.STOP_WORDS):
            decision = say(word, *live)
            self.assertEqual((decision.action, decision.target, decision.why, decision.job_id), (STOP, 2, "2", "j2"), word)

    def test_punctuation_spaces_and_case_do_not_matter(self):
        for text in ("停止。", " 停 ", "别做了！！", "Stop!", "CANCEL", "算了…", "撤回 ", "不用了~"):
            self.assertEqual(say(text, job(1)).action, STOP, text)

    def test_a_quote_names_the_track_to_stop(self):
        live = (job(1, started=400), job(2, started=100))
        decision = say("停下", *live, quote=Quote(track_route.Q_LIVE, 1, "j1"))
        self.assertEqual((decision.action, decision.target), (STOP, 1))
        decision = say("发错了", *live, quote=Quote(track_route.Q_MERGED, 1, "j1", "原话"))
        self.assertEqual((decision.action, decision.target), (STOP, 1))

    def test_a_quote_of_something_that_ended_stops_the_newest(self):
        live = (job(1, started=400), job(2, started=100))
        for kind in (track_route.Q_ENDED, track_route.Q_OWN, track_route.Q_UNKNOWN):
            self.assertEqual(say("停", *live, quote=Quote(kind)).target, 2, kind)
        self.assertEqual(say("停", *live, quote=Quote(track_route.Q_LIVE, 9, "j9")).target, 2)

    def test_a_stop_word_inside_a_sentence_is_not_a_stop(self):
        for text in ("停车场怎么收费", "别停，继续做", "不要停下来", "stop words 是什么", "算了算了算了算了算了"):
            self.assertNotEqual(say(text, job(1)).action, STOP, text)

    def test_a_repeated_word_is_not_a_word_of_the_table(self):
        self.assertEqual(say("停止，停止，停止", job(1)).action, ASK_JUDGE)

    def test_the_stop_beats_a_paused_answer_and_a_bare_message(self):
        self.assertEqual(say("停", job(1, "paused")).action, STOP)
        self.assertEqual(say("停", job(1), bare=True).action, STOP)


class RuleThreeTest(unittest.TestCase):
    def test_alone_on_a_merged_message_is_a_new_request_with_its_text(self):
        quote = Quote(track_route.Q_MERGED, 1, "j1", "  被并进去的原话 ")
        for word in sorted(track_route.ALONE_WORDS):
            decision = say(word, job(1), quote=quote)
            self.assertEqual((decision.action, decision.target, decision.why, decision.job_id, decision.text),
                             (NEW, None, "3", "j1", "被并进去的原话"), word)

    def test_the_task_it_was_merged_into_may_have_ended(self):
        decision = say("单独答。", job(2), quote=Quote(track_route.Q_MERGED, 1, "j9", "原话"))
        self.assertEqual((decision.action, decision.job_id), (NEW, "j9"))

    def test_alone_on_something_else_is_not_rule_three(self):
        self.assertEqual(say("单独答", job(1), quote=Quote(track_route.Q_LIVE, 1, "j1")).why, "4")
        self.assertEqual(say("单独答", job(1), quote=Quote(track_route.Q_ENDED)).why, "5")
        self.assertEqual(say("单独答", job(1)).why, "9")

    def test_a_merged_message_with_any_other_words_is_a_follow_up(self):
        decision = say("好的", job(1), quote=Quote(track_route.Q_MERGED, 1, "j1", "原话"))
        self.assertEqual((decision.action, decision.why), (NEW, "5"))

    def test_an_empty_quote_text_falls_back_to_the_message(self):
        self.assertEqual(say("单独答", job(1), quote=Quote(track_route.Q_MERGED, 1, "j1")).text, "单独答")


class RuleFourTest(unittest.TestCase):
    def test_a_live_card_takes_the_message_by_its_track(self):
        live = (job(1), job(2, "queued", started=50))
        decision = say("再加一列", *live, quote=Quote(track_route.Q_LIVE, 2, "j2"))
        self.assertEqual((decision.action, decision.target, decision.why, decision.job_id, decision.answers),
                         (MERGE, 2, "4", "j2", False))

    def test_a_card_of_a_waiting_task_is_an_answer(self):
        decision = say("美式", job(1), job(2, "paused"), quote=Quote(track_route.Q_LIVE, 2, "j2"))
        self.assertEqual((decision.action, decision.target, decision.answers), (MERGE, 2, True))

    def test_the_task_key_wins_over_the_track(self):
        decision = say("嗯", job(1), job(2), quote=Quote(track_route.Q_LIVE, 1, "j2"))
        self.assertEqual(decision.target, 2)

    def test_a_card_found_by_track_alone(self):
        self.assertEqual(say("嗯嗯", job(3), quote=Quote(track_route.Q_LIVE, 3)).target, 3)

    def test_a_card_whose_task_ended_meanwhile_is_a_follow_up(self):
        decision = say("再加一行", job(1), quote=Quote(track_route.Q_LIVE, 9, "j9"))
        self.assertEqual((decision.action, decision.why), (NEW, "5"))

    def test_the_quote_beats_the_short_answer_and_the_correction(self):
        live = (job(1, "paused", owner=5), job(2, owner=5))
        decision = say("不对，是英文", *live, quote=Quote(track_route.Q_LIVE, 2, "j2"))
        self.assertEqual((decision.target, decision.why), (2, "4"))


class RuleFiveTest(unittest.TestCase):
    def test_a_quote_of_an_ended_answer_or_an_old_message_is_a_follow_up(self):
        for kind in (track_route.Q_ENDED, track_route.Q_OWN):
            for text in ("再详细点", "好的", "不对，是英文", "还有"):
                decision = say(text, job(1, owner=5), quote=Quote(kind, text="原文"))
                self.assertEqual((decision.action, decision.target, decision.why), (NEW, None, "5"), (kind, text))

    def test_an_unknown_quote_is_left_to_the_judge_or_new(self):
        self.assertEqual(say("这个怎么处理", job(1), quote=Quote(track_route.Q_UNKNOWN)).action, ASK_JUDGE)
        self.assertEqual(say("这个怎么处理", job(1), quote=Quote(track_route.Q_UNKNOWN), judge=False).action, NEW)

    def test_a_quote_kind_the_module_does_not_know_is_an_unknown_quote(self):
        self.assertEqual(say("嗯", job(1), quote=Quote("something new")).action, ASK_JUDGE)


class RuleSixTest(unittest.TestCase):
    paused = (job(1, "paused"),)

    def test_a_short_answer_to_the_one_waiting_task(self):
        for text in ("美式", "好的", "可以，就按你说的做", "x" * 20):
            decision = say(text, *self.paused)
            self.assertEqual((decision.action, decision.target, decision.why, decision.answers), (MERGE, 1, "6", True), text)

    def test_the_task_that_waits_is_not_always_the_newest(self):
        decision = say("好的", job(1, "paused", started=500), job(2, started=50))
        self.assertEqual((decision.target, decision.job_id), (1, "j1"))

    def test_options_are_answers_of_any_length(self):
        for text in ("选 B", "选a", "2", "12", "A", "第二个", "第 3 个", "yes", "OK", "是", "不对", "都行"):
            self.assertEqual(say(text, *self.paused).why, "6", text)

    def test_a_question_mark_or_a_long_text_is_not_an_answer(self):
        for text in ("你问的是哪个仓库？", "which one?", "x" * 21, "顺便帮我查一下明天示例城市的天气和穿衣建议，谢谢"):
            self.assertEqual(say(text, *self.paused).action, ASK_JUDGE, text)

    def test_two_waiting_tasks_have_no_one_answer(self):
        self.assertEqual(say("美式", job(1, "paused"), job(2, "paused")).action, ASK_JUDGE)

    def test_no_waiting_task_has_nothing_to_answer(self):
        self.assertEqual(say("好的", job(1), job(2)).action, ASK_JUDGE)

    def test_an_attachment_message_is_not_the_answer_of_this_rule(self):
        decision = say("", *self.paused, bare=True)
        self.assertEqual((decision.action, decision.why), (MERGE, "7"))
        self.assertEqual(say("   ", *self.paused).action, ASK_JUDGE)  # No words, nothing flagged bare: the judge or new.


class RuleSevenTest(unittest.TestCase):
    def test_a_bare_message_joins_the_newest_track(self):
        decision = say("https://example.com/a", job(1, started=400), job(2, started=60), job(3, started=200), bare=True)
        self.assertEqual((decision.action, decision.target, decision.why, decision.job_id), (MERGE, 2, "7", "j2"))

    def test_a_tie_in_start_time_takes_the_larger_track(self):
        self.assertEqual(say("", job(1, started=100), job(3, started=100), job(2, started=100), bare=True).target, 3)

    def test_bare_with_a_quote_follows_the_quote(self):
        self.assertEqual(say("", job(1), job(2), quote=Quote(track_route.Q_ENDED), bare=True).action, NEW)
        self.assertEqual(say("", job(1), job(2), quote=Quote(track_route.Q_LIVE, 1, "j1"), bare=True).target, 1)


class RuleEightTest(unittest.TestCase):
    def test_a_correction_word_within_a_minute(self):
        for text in ("不对，是英文版", "不是这个", "错了", "我是说用表格", "我的意思是只要明天的", "补充一下", "另外把标题放大",
                     "改成 Markdown", "换成 CSV", "等一下，先别提交", "对，就这样", "对,就这样"):
            decision = say(text, job(1, owner=30))
            self.assertEqual((decision.action, decision.target, decision.why), (MERGE, 1, "8"), text)

    def test_the_prefixes_are_the_table(self):
        for prefix in track_route.CORRECTION_PREFIXES:
            self.assertEqual(say(prefix + "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", job(1, owner=10)).why, "8", prefix)

    def test_also_is_not_a_correction_word(self):
        for text in ("还有，再加一列", "还有", "还有一个问题", "然后呢", "再加一列"):
            self.assertEqual(say(text, job(1, owner=10)).action, ASK_JUDGE, text)

    def test_a_word_that_only_starts_like_one_is_not_one(self):
        for text in ("对话框的样式能改吗", "对了，顺便问一下", "补", "错", "另一个问题", "换个思路", "改一下"):
            self.assertEqual(say(text, job(1, owner=10)).action, ASK_JUDGE, text)

    def test_only_at_the_start(self):
        self.assertEqual(say("我觉得不对，是英文版", job(1, owner=10)).action, ASK_JUDGE)

    def test_the_minute_counts_from_the_newest_tracks_last_owner_message(self):
        self.assertEqual(say("不对", job(1, owner=59)).why, "8")
        self.assertEqual(say("不对", job(1, owner=60)).action, ASK_JUDGE)
        self.assertEqual(say("不对", job(1, owner=200)).action, ASK_JUDGE)
        self.assertEqual(say("不对", job(1, owner=0)).why, "8")
        # The older track's recent message does not count: the target is the newest track.
        self.assertEqual(say("不对", job(1, started=500, owner=5), job(2, started=50, owner=300)).action, ASK_JUDGE)

    def test_an_unknown_time_or_a_clock_that_ran_back_is_not_recent(self):
        self.assertEqual(say("不对", LiveJob(1, "j1", "running", "x", NOW - 100, 0)).action, ASK_JUDGE)
        self.assertEqual(say("不对", LiveJob(1, "j1", "running", "x", NOW - 100, NOW + 30)).action, ASK_JUDGE)

    def test_the_answer_rule_comes_first(self):
        decision = say("不对", job(1, "paused", owner=5))
        self.assertEqual((decision.why, decision.answers), ("6", True))


class RuleNineTest(unittest.TestCase):
    def test_the_rest_goes_to_the_judge_with_no_target(self):
        decision = say("帮我写一首关于春天的短诗", job(1), job(2))
        self.assertEqual(decision, Decision(ASK_JUDGE, None, "9"))

    def test_without_a_judge_it_is_new(self):
        decision = say("帮我写一首关于春天的短诗", job(1), judge=False)
        self.assertEqual(decision, Decision(NEW, None, "9"))

    def test_the_judge_flag_does_not_touch_the_earlier_rules(self):
        self.assertEqual(say("停", job(1), judge=False).action, STOP)
        self.assertEqual(say("美式", job(1, "paused"), judge=False).action, MERGE)


class PropertyTest(unittest.TestCase):
    def test_only_rule_one_answers_legacy_and_nothing_raises(self):
        texts = ("", " ", "停", "不对", "对，", "好", "选 A", "1", "单独答", "帮我写首诗", "？", "还有", "x" * 400, "停止\n", "​")
        lives = [(), (job(1),), (job(1, "paused"),), (job(1, owner=5), job(2, "queued", owner=5)),
                 (job(1, "paused"), job(2, "paused")), (job(4, "starting", started=0, owner=0),)]
        quotes = [Quote(kind, track, job_id, text) for kind in sorted(track_route.QUOTE_KINDS) + ["bogus"]
                  for track, job_id, text in ((None, "", ""), (1, "j1", "原文"), (9, "j9", ""))]
        for text, live, quote, bare, judge, enabled in itertools.product(
                texts, lives, quotes, (False, True), (False, True), (False, True)):
            decision = route(Message(text, live, quote, enabled, NOW, bare, judge))
            self.assertIn(decision.action, {LEGACY, STOP, MERGE, NEW, ASK_JUDGE})
            self.assertEqual(decision.action == LEGACY, not enabled or not live, (text, live, quote, enabled))
            if decision.action in (MERGE, STOP):
                self.assertIn(decision.target, {j.track for j in live})
            else:
                self.assertIsNone(decision.target)
            if decision.action == ASK_JUDGE:
                self.assertTrue(judge)
            self.assertTrue(decision.why)

    def test_the_words_are_clean(self):
        for words in (track_route.STOP_WORDS, track_route.ALONE_WORDS, track_route.OPTION_WORDS):
            for word in words:
                self.assertEqual(word, track_route.squash(word))
        self.assertTrue(all(len(word) <= track_route.STOP_MAX for word in track_route.STOP_WORDS))
        self.assertTrue(track_route.STOP_WORDS.isdisjoint(track_route.ALONE_WORDS))

    def test_the_module_imports_only_the_standard_library(self):
        tree = ast.parse((REPO / "covey" / "core" / "track_route.py").read_text(encoding="utf-8"))
        names = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        names |= {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        self.assertEqual(sorted(names - set(sys.stdlib_module_names)), [])

    def test_squash(self):
        self.assertEqual(track_route.squash(" 停 止，！Stop."), "停止stop")


class CorpusTest(unittest.TestCase):
    def test_every_case_of_the_synthetic_corpus_is_decided_by_the_rule_it_names(self):
        module = load_eval()
        cases = module.load_corpus(module.DEFAULT_CORPUS)
        self.assertGreaterEqual(len(cases), 60)
        wrong = [(c["id"], c["rule"], d.why) for c in cases
                 for d in [route(module.message_of(c))] if d.why != c["rule"]]
        self.assertEqual(wrong, [])
        self.assertEqual({c["rule"] for c in cases}, {str(n) for n in range(1, 10)})

    def test_the_corpus_has_no_private_text(self):
        text = (REPO / "tests" / "fixtures" / "track_route" / "corpus.jsonl").read_text(encoding="utf-8")
        for marker in ("ou_", "oc_", "om_", "@", "/mnt/", "/home/", "http://"):
            self.assertNotIn(marker, text)

    def test_the_evaluation_counts_both_mistakes(self):
        module = load_eval()
        result = module.evaluate(module.load_corpus(module.DEFAULT_CORPUS))
        counts = result["counts"]
        self.assertEqual(counts["total"], counts["label_new"] + counts["label_merge"] + counts["label_stop"])
        self.assertEqual(result["stop_missed"], 0)
        self.assertEqual(result["stop_wrong"], 0)
        self.assertAlmostEqual(result["new_merged"], counts.get("new_merged", 0) / counts["label_new"])
        self.assertAlmostEqual(result["merge_new"], counts.get("merge_new", 0) / counts["label_merge"])
        worst = module.evaluate(module.load_corpus(module.DEFAULT_CORPUS), judge="merge")
        self.assertGreaterEqual(worst["new_merged"], result["new_merged"])
        self.assertLessEqual(worst["merge_new"], result["merge_new"])

    def test_the_model_judge_is_asked_for_exactly_the_cases_the_rules_leave_to_it(self):
        module = load_eval()
        cases = module.load_corpus(module.DEFAULT_CORPUS)
        asked = []

        def ask(case):
            asked.append(case["id"])
            return "merge" if case["label"] == "merge" else "new"

        result = module.evaluate(cases, "model", ask)
        expected = [c["id"] for c in cases if route(module.message_of(c)).action == ASK_JUDGE]
        self.assertEqual(asked, expected)
        self.assertEqual(result["counts"]["ask_judge"], len(expected))


if __name__ == "__main__":
    unittest.main()
