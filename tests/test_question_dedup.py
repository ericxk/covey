"""问前查重 (app/question_dedup.py, app/native_questions.py dispatch) and the older callers on the memory store (core/memstore.py):
the check only once core/memory_search.py has its similarity, the refusal in the same turn before anything is registered and at
most once per question even after a restart, the grey line on any other backend and for the high-risk classes, the lead given
the old answer, the third same-class question kept out of ``questions`` (live_question_proof), out of what the model gets and out
of the decisions, a rule only from the owner's own submit, 30 quiet days; and config_basis, triage's grounds and the lead's tools
reading short codes, the older ids and decisions.jsonl."""
from __future__ import annotations

import hashlib
import json
import os
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support import UNSET_OFF, make_context, make_job
from support.records import state_dir, write_job, write_topic

from covey.app import answers, config_basis, native_questions, project_context, project_tools, question_dedup, triage as flow
from covey.app.native_questions import NativeQuestions
from covey.core import memory as core_memory
from covey.core import memory_rank, memory_search, memory_usage, memstore, project_memory, projects, triage, turn_source
from covey.core.jobs import find_job_record

CHAT = "oc_owner_dm"
PID = "a1b2c3d4e5f6"
OTHER = "f6e5d4c3b2a1"
MEMBER = "ab" * 16
PLAIN = "cd" * 16
LEAD = projects.lead_topic_id(CHAT, PID)
NOW = time.time()
MERGE = {"id": "merge", "header": "合入", "question": "这次的改动现在合进主干吗？",
         "options": [{"label": "合进去"}, {"label": "先不合"}]}
DEPLOY = {"id": "deploy", "header": "部署", "question": "现在部署到现网吗？", "options": [{"label": "部署"}, {"label": "先不部署"}]}
COLOR = {"id": "color", "header": "配色", "question": "卡片的配色用哪一版？", "options": [{"label": "A 版"}, {"label": "B 版"}]}


def search(state_dir, query, context=None, limit=5):
    """eval_search as core/memory_search.py is to give it: refs of candidate entries (here every decision)."""
    return [{"ref": entry.ref, "sim": 1.0} for entry in memstore.entries(state_dir, kinds=("decision",), statuses=None)][:limit * 4]


def mutual(a, b, *, state_dir=None, corpus=None):
    """mutual_similarity's shape: (absolute similarity, covered); here the characters both share."""
    left, right = set(str(a)), set(str(b))
    sim = len(left & right) / max(len(left | right), 1)
    return sim, sim > 0.3


def similarity_ready(test):
    """core/memory_search.py with eval_search, mutual_similarity and DEFAULT_TAU_DUP (修订 2 landed)."""
    for name, value in (("eval_search", search), ("mutual_similarity", mutual), ("DEFAULT_TAU_DUP", 0.6)):
        patch = mock.patch.object(memory_search, name, value, create=True)
        patch.start()
        test.addCleanup(patch.stop)


def environment(test, **extra):
    env = {name: value for name, value in os.environ.items() if not name.startswith(("COVEY_", "LARK_BRIDGE_"))}
    patch = mock.patch.dict(os.environ, {**env, **UNSET_OFF, projects.ENV: "1", projects.MEMORY_ENV: "1", core_memory.ENV: "1",
                                         **extra}, clear=True)
    patch.start()
    test.addCleanup(patch.stop)


def answer(state, question, said, *, owner=PID, at=None, token=None):
    """An answer the owner gave before: a decision in decisions.jsonl, as remember_answer notes one."""
    scope = "project" if projects.valid_id(owner) else "topic"
    return memstore.add_decision(state, scope=scope, owner_key=owner, question=question["question"], answer=said,
                                 text=project_context.answer_line(question["question"], said),
                                 question_ref=f"{token}-1" if token else "", topic_key=MEMBER, chosen=said,
                                 category=question_dedup.category(question["question"]), polarity=question_dedup.polarity(said),
                                 origin={"topic_key": MEMBER, "topic_number": 12}, now=at or NOW - 3600)


class Spec(SimpleNamespace):
    pass


class Rpc:
    def __init__(self, live=True):
        self.native_blocking_questions = live
        self.refused = []
        self.session_id = "sid"

    def reject_question(self, request_id, reason):
        self.refused.append((request_id, reason))


class Case(unittest.TestCase):
    def setUp(self):
        self.state = state_dir(self)
        environment(self)
        projects.create(self.state, projects.new_record(chat_id=CHAT, name="改造", project_id=PID, now=NOW))
        projects.create(self.state, projects.new_record(chat_id=CHAT, name="别的", project_id=OTHER, now=NOW))
        write_topic(self.state, topic_id=LEAD, chat_id=CHAT, project=projects.project_field(PID, projects.LEAD))
        write_topic(self.state, topic_id=MEMBER, chat_id=CHAT, project=projects.project_field(PID, projects.MEMBER))
        write_topic(self.state, topic_id=PLAIN, chat_id=CHAT)
        self.logs = []

    def ctx(self, topic=MEMBER):
        return make_context(self.state.parent, state_dir=self.state, topic_id=topic)

    def native(self, harness="claude", refuses=True, leads=False):
        spec = Spec(name=harness, refuses_questions=refuses)
        questions = SimpleNamespace(triage=SimpleNamespace(leads=lambda ctx, record: leads))
        return NativeQuestions(SimpleNamespace(spec=spec), runtime=SimpleNamespace(log=self.logs.append), cards=None,
                               questions=questions)

    def job(self, topic=MEMBER, key="om_q1", job_id=1):
        write_job(self.state, key, topic_id=topic, chat_id=CHAT, job_id=job_id, status="running")
        checkpoint = self.state / f"cp-{key}.json"
        checkpoint.write_text("{}")
        return make_job(message_id=key, topic_id=topic, chat_id=CHAT, job_id=job_id, checkpoint_path=str(checkpoint),
                        bridge_run_id="run-1")

    def ask(self, native, job, questions, rpc=None, request="req-1", defer=False):
        """dispatch a question; (the records saved, the calls in order)."""
        calls = []
        real_register = native.register

        def register(*args, **kwargs):
            calls.append("register")
            return real_register(*args, **kwargs)

        with mock.patch.object(native, "register", side_effect=register), mock.patch.object(native, "show"), \
                mock.patch.object(native, "mirror"), mock.patch.object(native, "dedup", side_effect=lambda *a, **k: (
                    calls.append("dedup"), NativeQuestions.dedup(native, *a, **k))[1]):
            native.dispatch(self.ctx(job.topic_id), job, "question", {"request_id": request, "questions": questions,
                                                                      "isBlocking": True}, rpc, defer_question=defer)
        folder = self.state / "questions"
        saved = [json.loads(path.read_text()) for path in sorted(folder.glob("*.json"))] if folder.is_dir() else []
        return saved, calls


def ask_of(text, qid="q"):
    return {"id": qid, "question": text, "options": [{"label": "好"}, {"label": "先不"}]}


class RealSearchTest(Case):
    """M2 + M3 together (T870 Wave 2B): core/memory_search.py's real eval_search and mutual_similarity, nothing stubbed. The
    context keys are the ones eval_search reads (topic, project), the hit is eval_search's one-way sim at DEFAULT_TAU_DUP and
    both questions alike at MUTUAL_DUP: a question asked again in other words is found, one on the same subject that asks
    something else is not, nor one answered in another project."""

    def setUp(self):
        super().setUp()
        self.other_lead = projects.lead_topic_id(CHAT, OTHER)
        write_topic(self.state, topic_id=self.other_lead, chat_id=CHAT, project=projects.project_field(OTHER, projects.LEAD))
        corpus = [("Repair 能不能改题面？", "不能改，只能补测试"), ("r20 要不要整题重跑补进来？", "整题重跑"),
                  ("评审要不要加非阻塞 minor 档？", "加"), ("现网跑满 12 小时后再推 stable 吗？", "跑满再推"),
                  ("网页工作台对外怎么处理？", "先内部用"), ("v1.9 的 diff 看过后现在开跑吗？", "开跑"),
                  ("王钰的主力机器人 console 这次怎么处理？", "不动"), ("Golden 的主指标用哪把尺子？", "新尺子")]
        for n, (question, said) in enumerate(corpus):
            answer(self.state, {"question": question}, said, at=NOW - 3600 - n)
        answer(self.state, {"question": "e2e50b 要不要用新尺子重判一遍？"}, "重判", owner=OTHER)

    def check(self, text, topic=MEMBER):
        return question_dedup.check(self.ctx(topic), {"topic_id": topic}, [ask_of(text)]).get("q")

    def test_an_ordinary_topics_third_of_a_class_reads_the_other_ordinary_topics(self):
        # T870 Wave 3.5: a turn not the owner's reads only its own topic and project (core/memory_search.py _allowed); the
        # class of an ordinary topic is still every ordinary topic's, the Bridge reading them for the card.
        for n, tid in enumerate(("ef" * 32, "fa" * 32)):
            write_topic(self.state, topic_id=tid, chat_id=CHAT, title=f"普通话题{n}")
            memstore.add_decision(self.state, scope="topic", owner_key=tid, question=MERGE["question"], answer="合进去",
                                  text=project_context.answer_line(MERGE["question"], "合进去"), topic_key=tid, chosen="合进去",
                                  category=question_dedup.category(MERGE["question"]), polarity=question_dedup.polarity("合进去"),
                                  origin={"topic_key": tid, "topic_number": 20 + n}, now=NOW - (n + 1) * 86400)
        (extra,) = question_dedup.class3(self.ctx(PLAIN), {"topic_id": PLAIN}, [MERGE], now=NOW)
        self.assertEqual(extra["id"], question_dedup.EXTRA_ID)
        self.assertIsNone(self.check(MERGE["question"], topic=PLAIN))  # Asked again: only its own topic's answers count.

    def test_the_search_is_m2s_own(self):
        search, mutual_, tau = question_dedup.engine()
        self.assertIs(search, memory_search.eval_search)
        self.assertIs(mutual_, memory_search.mutual_similarity)
        self.assertEqual(tau, memory_search.DEFAULT_TAU_DUP)
        seen = []

        def spy(state_dir, query, context=None, limit=5):
            seen.append(dict(context or {}))
            return memory_search.eval_search(state_dir, query, context, limit)
        with mock.patch.object(memory_search, "eval_search", spy):
            self.check("Repair 环节可以改题面吗？")
        project = question_dedup.project_of(self.state, MEMBER)
        self.assertEqual((seen[0]["topic"], seen[0]["project"], tuple(seen[0]["kinds"]), seen[0]["tau"]),
                         (MEMBER, project, ("decision",), memory_search.DEFAULT_TAU_DUP))
        self.assertNotIn("topic_id", seen[0])

    def test_asked_again_in_other_words_is_found(self):
        for text, old in (("Repair 环节可以改题面吗？", "Repair 能不能改题面？"), ("r20 那道题要整题重跑再补进来吗？", "r20 要不要整题重跑补进来？")):
            hit = self.check(text)
            self.assertIsNotNone(hit, text)
            self.assertEqual(hit.question, old)
            self.assertGreaterEqual(hit.sim, memory_search.DEFAULT_TAU_DUP)

    def test_the_same_subject_asking_something_else_is_not(self):
        for text in ("Repair 环节的日志要保留几天？", "现在要不要弄一下这个？"):
            self.assertIsNone(self.check(text), text)

    def test_with_few_decisions_a_shared_noun_alone_is_only_a_grey_line_never_a_refusal(self):
        # Eight decisions: every word weighs about the same, 网页工作台 alone makes it look alike (on the live copy's 400
        # entries it is 0.18 and not even marked, verify/after/dedup-probe.json D02).
        question = ask_of("网页工作台要不要加夜间自动备份？")
        weak = self.check(question["question"])
        if weak is not None:
            self.assertLess(weak.mutual, question_dedup.REFUSE_MUTUAL)
            self.assertFalse(question_dedup.may_refuse([], question, weak))
        strong = self.check("r20 那道题要整题重跑再补进来吗？")
        self.assertGreaterEqual(strong.mutual, question_dedup.REFUSE_MUTUAL)
        self.assertTrue(question_dedup.may_refuse([], ask_of("r20 那道题要整题重跑再补进来吗？"), strong))

    def test_a_question_asked_again_is_refused_in_the_turn_with_the_real_search(self):
        rpc = Rpc()
        saved, calls = self.ask(self.native(), self.job(), [ask_of("r20 那道题要整题重跑再补进来吗？")], rpc)
        self.assertEqual((calls, saved), (["dedup"], []))
        ((_, reason),) = rpc.refused
        self.assertIn("整题重跑", reason)
        rpc = Rpc()  # A weak match: asked, with the grey line only.
        saved, _ = self.ask(self.native(), self.job(key="om_q2", job_id=2), [ask_of("网页工作台要不要加夜间自动备份？")], rpc,
                            request="req-2")
        self.assertEqual(rpc.refused, [])
        self.assertEqual(len(saved), 1)

    def test_an_old_answer_of_a_high_risk_class_is_never_handed_back(self):
        """T870 review: the new question's words say neither 删 nor 部署 (``other``), the old answer said 删掉: a grey line only,
        never a refusal in the turn, never ground for the lead."""
        answer(self.state, {"question": "旧的 checkpoint 目录能清一下吗？"}, "删掉吧")
        question = ask_of("旧的 checkpoint 目录能清一下吗？")
        self.assertFalse(question_dedup.risky(question))
        hit = self.check(question["question"])
        self.assertIsNotNone(hit)
        self.assertTrue(hit.risky)
        self.assertFalse(question_dedup.may_refuse([], question, hit))
        marks = question_dedup.marks({"q": hit})
        self.assertTrue(marks[question_dedup.MARK_KEY]["q"]["risky"])
        self.assertEqual(question_dedup.lead_line(marks), "")
        rpc = Rpc()
        saved, _ = self.ask(self.native(), self.job(key="om_q9", job_id=9), [question], rpc, request="req-9")
        self.assertEqual(rpc.refused, [])
        self.assertEqual(len(saved), 1)
        self.assertIn("q", saved[0][question_dedup.MARK_KEY])  # The grey line is still on the card.

    def test_an_answer_of_another_project_is_not_this_ones(self):
        self.assertIsNone(self.check("e2e50b 那批结果要用新尺子再判一遍吗？"))
        hit = self.check("e2e50b 那批结果要用新尺子再判一遍吗？", topic=self.other_lead)
        self.assertIsNotNone(hit)
        self.assertEqual(hit.answer, "重判")


class OnlyWithTheNewSimilarityTest(Case):
    def test_nothing_is_matched_while_memory_search_has_not_the_new_similarity(self):
        answer(self.state, MERGE, "合进去")
        for name in ("eval_search", "mutual_similarity"):
            patch = mock.patch.object(memory_search, name, None, create=True)
            patch.start()
            self.addCleanup(patch.stop)
        self.assertIsNone(question_dedup.engine())
        self.assertEqual(question_dedup.check(self.ctx(), {"topic_id": MEMBER}, [MERGE]), {})
        self.assertEqual(question_dedup.class3(self.ctx(), {"topic_id": MEMBER}, [MERGE]), [])
        rpc = Rpc()
        saved, _ = self.ask(self.native(), self.job(), [MERGE], rpc)
        self.assertEqual((rpc.refused, len(saved)), ([], 1))
        self.assertNotIn(question_dedup.MARK_KEY, saved[0])

    def test_once_it_has_them_a_question_answered_before_is_found_and_another_one_is_not(self):
        similarity_ready(self)
        old = answer(self.state, MERGE, "合进去")
        answer(self.state, MERGE, "合进去", owner=OTHER)  # Another project's: not this topic's.
        hits = question_dedup.check(self.ctx(), {"topic_id": MEMBER}, [MERGE, COLOR])
        self.assertEqual(list(hits), ["merge"])
        self.assertEqual((hits["merge"].ref, hits["merge"].code, hits["merge"].answer, hits["merge"].topic_number),
                         (old.ref, old.code, "合进去", 12))
        self.assertEqual(question_dedup.check(self.ctx(PLAIN), {"topic_id": PLAIN}, [MERGE]), {})  # Not its topic's or project's.
        memstore.supersede(self.state, old.ref, answer(self.state, COLOR, "A 版").ref)
        self.assertEqual(question_dedup.check(self.ctx(), {"topic_id": MEMBER}, [MERGE]), {})  # Replaced: no longer the answer.

    def test_categories_and_polarity(self):
        self.assertEqual([question_dedup.category(q["question"]) for q in (MERGE, DEPLOY, COLOR)], ["merge", "deploy", "sample"])
        self.assertEqual([question_dedup.polarity(a) for a in ("合进去", "先不合", "随便")], ["yes", "no", ""])
        self.assertTrue(question_dedup.risky(DEPLOY))
        self.assertFalse(question_dedup.risky(MERGE))


class RefuseInTheTurnTest(Case):
    def setUp(self):
        super().setUp()
        similarity_ready(self)
        self.old = answer(self.state, MERGE, "合进去")

    def test_a_backend_that_takes_it_back_in_the_turn_is_refused_before_anything_is_registered(self):
        rpc = Rpc()
        job = self.job()
        saved, calls = self.ask(self.native(), job, [MERGE], rpc)
        self.assertEqual(calls, ["dedup"])  # Refused before register: nothing left pending to hold the topic.
        self.assertEqual(saved, [])
        ((request, reason),) = rpc.refused
        self.assertEqual(request, "req-1")
        self.assertIn("合进去", reason)
        self.assertIn(self.old.code, reason)
        rows = find_job_record(self.state, job.message_id)[question_dedup.JOB_FIELD]
        self.assertEqual([(row["q"], row["ref"], row["request"]) for row in rows], [(question_dedup.q_hash(MERGE), self.old.ref, "req-1")])
        self.assertEqual(memory_usage.counts(self.state)[self.old.ref]["h"], memory_rank.SIGNAL_WEIGHTS["dedup"])  # Its signal.

    def test_at_most_once_per_question_even_after_a_restart(self):
        rpc = Rpc()
        self.ask(self.native(), self.job(), [MERGE], rpc)
        again = Rpc()
        saved, calls = self.ask(self.native(), self.job(key="om_q2", job_id=2), [MERGE], again, request="req-2")  # A new Bridge.
        self.assertEqual((again.refused, calls), ([], ["dedup", "register"]))
        self.assertEqual(saved[0][question_dedup.MARK_KEY]["merge"]["ref"], self.old.ref)  # The card says it instead.
        # The same event again (a redelivery) of the refused one registers nothing either.
        replay, _ = self.ask(self.native(), self.job(), [MERGE], Rpc(), request="req-1")
        self.assertEqual(len(replay), 1)

    def test_the_five_high_risk_classes_are_only_noted(self):
        answer(self.state, DEPLOY, "部署")
        rpc = Rpc()
        saved, calls = self.ask(self.native(), self.job(), [DEPLOY], rpc)
        self.assertEqual((rpc.refused, calls), ([], ["dedup", "register"]))
        self.assertIn("上次你答过：部署", saved[0][question_dedup.MARK_KEY]["deploy"]["line"])
        self.assertNotIn(question_dedup.EXTRA_KEY, saved[0])

    def test_one_question_of_two_answered_before_is_not_refused(self):
        rpc = Rpc()
        saved, _ = self.ask(self.native(), self.job(), [MERGE, COLOR], rpc)
        self.assertEqual(rpc.refused, [])
        self.assertEqual(list(saved[0][question_dedup.MARK_KEY]), ["merge"])

    def test_codex_agy_dsh_a_refusal_that_ends_the_run_and_a_checkpoint_question_get_the_grey_line_only(self):
        for native, rpc, defer in ((self.native("codex"), Rpc(live=False), False), (self.native("agy"), Rpc(live=False), False),
                                   (self.native("dsh"), Rpc(live=False), False), (self.native("ext", refuses=False), Rpc(), False),
                                   (self.native("claude"), None, True)):
            for path in (self.state / "questions").glob("*.json") if (self.state / "questions").is_dir() else ():
                path.unlink()
            saved, calls = self.ask(native, self.job(), [MERGE], rpc, defer=defer)
            self.assertEqual(calls, ["dedup", "register"], native.harness.spec.name)
            self.assertEqual(getattr(rpc, "refused", []), [])
            drawn = question_dedup.drawn(saved[0])
            self.assertIn("上次你答过：合进去（T12 · ", drawn["questions"][0]["question"])
            self.assertEqual(saved[0]["questions"], [MERGE])  # Only what is drawn has the line.

    def test_a_question_for_the_lead_is_never_refused_and_the_lead_gets_the_old_answer(self):
        rpc = Rpc()
        saved, calls = self.ask(self.native(leads=True), self.job(), [MERGE], rpc)
        self.assertEqual((rpc.refused, calls), ([], ["dedup", "register"]))
        self.assertIn(f"主人上次答过：{MERGE['question']}→合进去（{self.old.code}）", question_dedup.lead_line(saved[0]))
        self.assertNotIn(question_dedup.EXTRA_KEY, saved[0])

    def test_the_lead_may_have_it_says_triage_without_writing_anything(self):
        environment(self, **{triage.ENV: "1"})
        record = {"topic_id": MEMBER, "chat_id": CHAT, "questions": [MERGE], "blocking": True, "status": "pending"}
        projects.update(self.state, PID, lambda data: data.update(state=projects.ACTIVE, lead_topic_id=LEAD, threads=[MEMBER]))
        lead = flow.Triage(runtime=SimpleNamespace(log=self.logs.append), cards=None)
        self.assertTrue(lead.leads(self.ctx(), record))
        self.assertFalse(lead.leads(self.ctx(), {**record, "questions": [DEPLOY]}))  # A gate word: the owner's.
        self.assertFalse(lead.leads(self.ctx(PLAIN), {**record, "topic_id": PLAIN}))
        self.assertFalse((self.state / "questions").exists())


class ThirdOfAClassTest(Case):
    def setUp(self):
        super().setUp()
        similarity_ready(self)
        answer(self.state, MERGE, "合进去", at=NOW - 3 * 86400)
        answer(self.state, MERGE, "合进去吧", at=NOW - 86400)

    def test_the_extra_question_is_beside_the_questions_and_the_proof_is_unchanged(self):
        saved, _ = self.ask(self.native("codex"), self.job(), [MERGE], Rpc(live=False))
        record = saved[0]
        self.assertEqual(record["questions"], [MERGE])
        (extra,) = record[question_dedup.EXTRA_KEY]
        self.assertEqual((extra["id"], [o["label"] for o in extra["options"]]),
                         (question_dedup.EXTRA_ID, [question_dedup.KEEP_ASKING, question_dedup.FOLLOW]))
        proof = hashlib.sha256(json.dumps([MERGE], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        self.assertEqual(hashlib.sha256(json.dumps(record["questions"], ensure_ascii=False, sort_keys=True).encode()).hexdigest(), proof)
        drawn = question_dedup.drawn(record)
        self.assertEqual([q["id"] for q in drawn["questions"]], ["merge", question_dedup.EXTRA_ID])
        self.assertNotIn("class", drawn["questions"][1])
        self.assertEqual(question_dedup.drawn(drawn), drawn)  # Drawn once only, however many callers draw it.
        # A repaint draws the record as saved (T870): the extra question is on the form all the same.
        from covey.cards.question_cards import question_element
        shown = {**record, "display_pending": False, "body_delivery_confirmed": True}  # As it is once its card is out.
        painted = json.dumps(question_element(shown), ensure_ascii=False)
        self.assertIn(question_dedup.KEEP_ASKING, painted)
        self.assertIn(question_dedup.FOLLOW, painted)
        self.assertEqual(painted.count(question_dedup.FOLLOW), json.dumps(question_element({**drawn, "display_pending": False, "body_delivery_confirmed": True}), ensure_ascii=False).count(question_dedup.FOLLOW))

    def test_not_for_a_high_risk_class_nor_with_two_answers_that_disagree(self):
        answer(self.state, DEPLOY, "部署", at=NOW - 2 * 86400)
        answer(self.state, DEPLOY, "部署", at=NOW - 86400)
        self.assertEqual(question_dedup.class3(self.ctx(), {"topic_id": MEMBER}, [DEPLOY]), [])
        answer(self.state, COLOR, "A 版", at=NOW - 2 * 86400)
        answer(self.state, COLOR, "不要 A 版", at=NOW - 86400)
        self.assertEqual(question_dedup.class3(self.ctx(), {"topic_id": MEMBER}, [COLOR]), [])

    def submit(self, record, fields):
        given, response = answers.submitted_answer(record, fields)
        return {**record, "answers": given, **response, "status": "submitting"}

    def test_its_pick_is_kept_apart_from_the_answers_the_model_and_the_decisions(self):
        saved, _ = self.ask(self.native("codex"), self.job(), [MERGE], Rpc(live=False))
        record = self.submit(saved[0], {"option_0": "0", "option_1": "1"})
        self.assertEqual(record["answers"], {"merge": {"answers": ["合进去"]}})
        self.assertEqual(record[question_dedup.EXTRA_ANSWERS], {question_dedup.EXTRA_ID: question_dedup.FOLLOW})
        shown, text, delivered = answers.answer_message(record, record["answers"], "", [])
        self.assertNotIn("以后这类", shown + text)
        self.assertEqual(list(delivered), ["merge"])
        before = {entry.ref for entry in memstore.decisions(self.state, "project", PID)}
        project_context.remember_answer(self.state, MEMBER, record, self.logs.append, owners=True)
        new = [entry for entry in memstore.decisions(self.state, "project", PID) if entry.ref not in before]
        self.assertEqual([entry.text for entry in new], [project_context.answer_line(MERGE["question"], "合进去")])
        (rule,) = memstore.entries(self.state, scope="project", owner_key=PID, kinds=("rule",))
        self.assertEqual((rule.said, rule.by), ("owner", "bridge"))
        self.assertIn("合进去", rule.text)
        self.assertEqual(question_dedup.class3(self.ctx(), {"topic_id": MEMBER}, [MERGE]), [])  # Quiet now.

    def test_a_member_of_the_group_submitting_writes_no_rule(self):
        """T870 Wave 3.5 (Fable P1-3): anybody the group lets in may submit the card; only the owner's own pick of 以后按这次的做
        is 你定的 (app/questions.py owner_submitted: the click's operator_id is the owner's)."""
        saved, _ = self.ask(self.native("codex"), self.job(), [MERGE], Rpc(live=False))
        record = self.submit(saved[0], {"option_0": "0", "option_1": "1"})
        before = memstore.decisions(self.state, "project", PID)
        project_context.remember_answer(self.state, MEMBER, record, self.logs.append)  # Not known to be the owner's.
        self.assertEqual(memstore.entries(self.state, scope="project", owner_key=PID, kinds=("rule",)), [])
        self.assertEqual(memstore.decisions(self.state, "project", PID), before)  # Nor is the answer his decision (W0-1).
        from covey.app.questions import Questions
        access = SimpleNamespace(is_owner=lambda open_id: open_id == "ou_owner")
        asker = SimpleNamespace(runtime=SimpleNamespace(access=access))
        self.assertEqual([Questions.owner_submitted(asker, by) for by in ("ou_owner", "ou_member", "", None)],
                         [True, False, False, False])

    def test_the_lead_answering_never_writes_a_rule(self):
        saved, _ = self.ask(self.native("codex"), self.job(), [MERGE], Rpc(live=False))
        record = self.submit(saved[0], {"option_0": "0", "option_1": "1"})
        record["triage"] = {"state": triage.HELD, "at": NOW, "deadline": NOW + 180, "project": PID}
        project_context.remember_answer(self.state, MEMBER, record, self.logs.append)
        self.assertEqual(memstore.entries(self.state, scope="project", owner_key=PID, kinds=("rule",)), [])
        fields = answers.answer_fields(saved[0], {"merge": "合进去"}, flow.NOTE)  # What the lead's answer sends: no extra field.
        self.assertEqual(answers.extra_answers(saved[0], fields), {})

    def test_keep_asking_is_quiet_for_thirty_days(self):
        saved, _ = self.ask(self.native("codex"), self.job(), [MERGE], Rpc(live=False))
        record = self.submit(saved[0], {"option_0": "0", "option_1": "0"})
        project_context.remember_answer(self.state, MEMBER, record, self.logs.append, owners=True)
        self.assertEqual(memstore.entries(self.state, scope="project", owner_key=PID, kinds=("rule",)), [])
        self.assertEqual(question_dedup.class3(self.ctx(), {"topic_id": MEMBER}, [MERGE]), [])
        later = time.time() + (question_dedup.CLASS3_QUIET_DAYS + 1) * 86400
        self.assertEqual(question_dedup.quiet_until(self.state, record[question_dedup.EXTRA_KEY][0]["class"]["key"]) < later, True)


class OrdinaryTopicTest(Case):
    def test_an_ordinary_topics_answer_is_a_decision_of_the_topic(self):
        record = {"token": "0123456789abcdef", "topic_id": PLAIN, "questions": [COLOR], "answers": {"color": {"answers": ["A 版"]}}}
        project_context.remember_answer(self.state, PLAIN, record, self.logs.append, owners=True)
        (entry,) = memstore.decisions(self.state, "topic", PLAIN)
        self.assertEqual((entry.scope, entry.said, entry.origin["question"]), ("topic", "owner", "0123456789abcdef-1"))
        self.assertEqual(memstore.decisions(self.state, "project", PID), [])


class OlderCallersTest(Case):
    def setUp(self):
        super().setUp()
        self.addCleanup(lambda: None)

    def test_config_basis_takes_short_codes_the_older_ids_and_decisions(self):
        rule = memstore.add(self.state, kind="rule", scope="global", text="监视进程这类事用 Codex", said="owner")
        lead_note = memstore.add(self.state, kind="note", scope="global", text="跑批这类事用 Claude", said="summary", by="learn")
        write_job(self.state, "om_b1", topic_id=LEAD, chat_id=CHAT, job_id=3, status="running",
                  memory_ids=[{"n": "M12", "ref": lead_note.ref}])
        with mock.patch("covey.app.butler_handoff.asked", return_value=SimpleNamespace(words="随便")):
            self.assertEqual(config_basis.check(self.state, "om_b1", f"按 {rule.code}")[1], "")
            self.assertEqual(config_basis.check(self.state, "om_b1", f"按{lead_note.code}")[1], "")  # Carried this turn.
            self.assertEqual(config_basis.check(self.state, "om_b1", "按 M12")[1], "")
            self.assertTrue(config_basis.check(self.state, "om_b2", f"按 {lead_note.code}")[1])  # Not the owner's, not carried.
            self.assertTrue(config_basis.check(self.state, "om_b1", "按 RZZZZ")[1])
            answer(self.state, MERGE, "用 Codex 来合")
            self.assertEqual(config_basis.check(self.state, "om_b1", "用 Codex 来合", project_id=PID)[1], "")

    def test_config_basis_takes_a_principle_by_its_old_id_once_it_moved(self):
        moved = memstore.add(self.state, kind="rule", scope="global", text="改代码用 Claude", said="owner", aliases=("principle:p1a2b3c",))
        with mock.patch("covey.app.butler_handoff.asked", return_value=SimpleNamespace(words="")):
            self.assertEqual(config_basis.check(self.state, "om_x", "按 p1a2b3c"), (config_basis.short(moved.text), ""))

    def env(self):
        return SimpleNamespace(state_dir=self.state, runtime=SimpleNamespace(log=self.logs.append, access=None))

    def test_triage_grounds_take_codes_older_ids_and_decisions_jsonl(self):
        record = projects.read_record(self.state, PID)
        decision = answer(self.state, MERGE, "合进去", token="0123456789abcdef")  # Where the Bridge took it in: the card.
        legacy, _ = project_memory.add(self.state, PID, "主人答：先迁哪年？→2024", "decision", "owner_answer", now=NOW,
                                       origin={"question": "f" * 16, "index": 1})
        found = flow.grounds(self.env(), record, [decision.code, legacy["id"]])
        self.assertEqual([(item["level"], item["kind"]) for item in found], [(turn_source.LEVEL_OWNER, "memory")] * 2)
        self.assertEqual(found[0]["code"], decision.code)
        self.assertIn(decision.code, flow.ground_note(found))
        self.assertEqual(len(flow.grounds(self.env(), record, [decision.ref.rsplit(":", 1)[-1]])), 1)  # Its id, as the pack shows it without a code.
        theirs = answer(self.state, MERGE, "合进去", owner=OTHER, token="1" * 16)
        with self.assertRaisesRegex(ValueError, "不是这个专项的记忆"):
            flow.grounds(self.env(), record, [theirs.code])
        memstore.settle(self.state, decision.ref)
        with self.assertRaisesRegex(ValueError, "历史"):
            flow.grounds(self.env(), record, [decision.code])

    def lead_request(self, **args):
        write_job(self.state, "lead-job", topic_id=LEAD, chat_id=CHAT, job_id=9, status="running")
        return SimpleNamespace(job_key="lead-job", args=args, id="r1")

    def test_project_remember_takes_rule_and_note_with_a_title_and_still_the_older_kinds(self):
        from covey.core import butler

        self.addCleanup(butler.configure_state, butler.configure_state(self.state))
        self.assertTrue({"rule", "note"} <= set(project_tools.MEMORY_KINDS))  # app/butler_memory.py passes them on then.
        self.assertTrue({"decision", "constraint", "contact", "fact"} <= set(project_tools.MEMORY_KINDS))
        env = SimpleNamespace(state_dir=self.state)
        done = project_tools.project_remember(env, self.lead_request(kind="rule", text="发版前先跑全量测试", title="发版前跑全量"))
        entry = memstore.find(self.state, done["data"]["id"])
        self.assertEqual((entry.kind, entry.title, entry.said, entry.by, entry.scope), ("rule", "发版前跑全量", "summary", "lead", "project"))
        self.assertEqual(memstore.review_items(self.state), [])  # T914 M1: the owner does not review the lead's rules one by one.
        self.assertNotIn("待审核", json.dumps(done, ensure_ascii=False))
        from covey.app import memory_turn
        from covey.app.prompts import memory_rules_block
        carried, left = memory_turn.rules_of(self.state, project=PID)
        self.assertIn("发版前先跑全量测试（负责人记的）", memory_rules_block(carried, left))  # In force at once, marked the lead's.
        note = project_tools.project_remember(env, self.lead_request(kind="note", text="新库在二号机房"))
        self.assertEqual(memstore.find(self.state, note["data"]["id"]).kind, "note")
        with self.assertRaisesRegex(ValueError, "title 最多"):
            project_tools.project_remember(env, self.lead_request(kind="note", text="别的事实", title="长" * 41))
        old = project_tools.project_remember(env, self.lead_request(kind="contact", text="DBA 是值班的那位"))
        self.assertEqual(project_memory.live(self.state, projects.read_record(self.state, PID))[-1]["id"], old["data"]["id"])

    def test_project_memory_revise_names_a_decision_of_decisions_jsonl_by_its_code(self):
        from covey.core import butler

        self.addCleanup(butler.configure_state, butler.configure_state(self.state))
        env = SimpleNamespace(state_dir=self.state)
        decision = answer(self.state, MERGE, "合进去")
        with self.assertRaisesRegex(ValueError, "不能改正文"):
            project_tools.project_memory_revise(env, self.lead_request(op="update", id=decision.code, text="不合"))
        with self.assertRaisesRegex(ValueError, "主人更新的答复"):
            project_tools.project_memory_revise(env, self.lead_request(op="supersede", id=decision.code, text="不合"))
        project_tools.project_memory_revise(env, self.lead_request(op="retire", id=decision.code, why="合完了"))
        self.assertEqual(memstore.find(self.state, decision.ref).status, "done")
        theirs = answer(self.state, MERGE, "合进去", owner=OTHER)
        with self.assertRaisesRegex(ValueError, "不是这个专项的记忆"):
            project_tools.project_memory_revise(env, self.lead_request(op="retire", id=theirs.code))


QUOTA_OLD = {"id": "quota", "header": "配额", "question": "+2 核/+4 GiB 配额申请要提吗？",
             "options": [{"label": "提"}, {"label": "先不提"}]}
QUOTA_NEW = {**QUOTA_OLD, "question": "+16 核/+32 GiB 配额申请要提吗？"}
FIGURE_PAIRS = ((QUOTA_OLD["question"], QUOTA_NEW["question"]), ("版本v2k 现在发给同事吗？", "版本v2l 现在发给同事吗？"),
                ("!304 现在合进主干吗？", "!305 现在合进主干吗？"))


class FigureSignatureTest(Case):
    """RT-5② (WAVE12 W2-C, live sample 171b294954): a question asked after one alike in words but with other figures (an
    amount, a version, a merge request) is another question: never refused in the turn, no grey line, nothing for the lead."""

    def test_the_signature_tells_the_three_pairs_apart_and_folds_only_case_spaces_and_a_plus(self):
        for old, new in FIGURE_PAIRS:
            self.assertNotEqual(question_dedup.params_signature(old), question_dedup.params_signature(new), (old, new))
        self.assertEqual(question_dedup.params_signature(QUOTA_NEW["question"]), ("16核", "32gib"))
        self.assertEqual(question_dedup.params_signature("版本v2l 要发吗"), ("v2l",))  # No CJK character takes the version's edge.
        self.assertEqual(question_dedup.params_signature("话题T912合吗"), ("t912",))
        for one, other in (("+16 核", "16核"), ("V1.9 的 diff", "v1.9 的diff"), ("50 % 流量", "50%流量"),
                           ("Repair 能不能改题面？", "Repair 环节可以改题面吗？"), ("r20 要不要重跑？", "r20 那道题要重跑吗？")):
            self.assertEqual(question_dedup.params_signature(one), question_dedup.params_signature(other), (one, other))
        self.assertEqual(question_dedup.params_signature(""), ())

    def test_other_figures_are_no_hit_and_the_same_figures_still_are(self):
        similarity_ready(self)
        for n, (old, new) in enumerate(FIGURE_PAIRS):
            answer(self.state, {"question": old}, "先不", at=NOW - 3600 - n)
        for old, new in FIGURE_PAIRS:
            self.assertEqual(question_dedup.check(self.ctx(), {"topic_id": MEMBER}, [ask_of(new)]), {}, new)
            hit = question_dedup.check(self.ctx(), {"topic_id": MEMBER}, [ask_of(old)]).get("q")
            self.assertIsNotNone(hit, old)  # Asked again with the same figures: found as before.
            self.assertEqual(hit.question, old)

    def test_a_refusing_backend_asks_it_with_no_grey_line_and_the_lead_gets_no_old_answer(self):
        similarity_ready(self)
        answer(self.state, QUOTA_OLD, "提")
        rpc = Rpc()
        saved, calls = self.ask(self.native(), self.job(), [QUOTA_NEW], rpc)
        self.assertEqual((rpc.refused, calls), ([], ["dedup", "register"]))
        self.assertNotIn(question_dedup.MARK_KEY, saved[0])
        self.assertNotIn("上次你答过", json.dumps(question_dedup.drawn(saved[0]), ensure_ascii=False))
        self.assertEqual(question_dedup.lead_line(saved[0]), "")
        rpc = Rpc()  # The old one asked again has its grey line as before (配额 is a gate word: never refused).
        saved, _ = self.ask(self.native(), self.job(key="om_q2", job_id=2), [QUOTA_OLD], rpc, request="req-2")
        (again,) = [row for row in saved if row["questions"] == [QUOTA_OLD]]
        self.assertIn("上次你答过：提", again[question_dedup.MARK_KEY]["quota"]["line"])
        old, new = FIGURE_PAIRS[2]  # Not of the five: one with the same figures is refused in the turn, one with others asked.
        answer(self.state, {"question": old}, "合进去", at=NOW - 7200)
        for n, (text, refused) in enumerate(((new, 0), (old, 1)), 3):
            rpc = Rpc()
            self.ask(self.native(), self.job(key=f"om_q{n}", job_id=n), [ask_of(text)], rpc, request=f"req-{n}")
            self.assertEqual(len(rpc.refused), refused, text)

    def test_with_the_real_search_the_live_sample_is_no_longer_matched(self):
        answer(self.state, QUOTA_OLD, "提")
        answer(self.state, {"question": "Repair 能不能改题面？"}, "不能改，只能补测试", at=NOW - 7200)
        check = lambda text: question_dedup.check(self.ctx(), {"topic_id": MEMBER}, [ask_of(text)]).get("q")  # noqa: E731
        self.assertIsNone(check(QUOTA_NEW["question"]))
        self.assertIsNotNone(check("+2 核/+4 GiB 的配额申请现在要提吗？"))


class NoInternalNamesTest(unittest.TestCase):
    def test_the_module_says_no_backend_by_its_internal_name_and_no_plan_file(self):
        words = Path(question_dedup.__file__).read_text(encoding="utf-8") + Path(native_questions.__file__).read_text(encoding="utf-8")
        for word in ("MASO", "FINAL-SPEC"):
            self.assertNotIn(word, words)


if __name__ == "__main__":
    unittest.main()
