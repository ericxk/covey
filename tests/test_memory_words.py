"""What the owner reads where a reply names a memory short code (core/memory_words.py, T969): the entry's title, never the
code; a code that only points inside brackets goes with its brackets; code, links, paths, commits and codes no entry has
stay as they are; a decision is never a link; only in the owner's private chat (a 代答 topic and any group keep the words
as they are), and a question's ids and option labels, which answers are matched by, never change."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace

from covey.core import memory_words, memstore

OWNER, GROUP = "oc_owner_dm", "oc_some_group"
URL = "https://page.example/w/memory"


def memory_on(case):
    """The memory on for ``case`` (the tests' support switches it off by default)."""
    patch = unittest.mock.patch.dict(os.environ, {"COVEY_MEMORY": "1"})
    patch.start()
    case.addCleanup(patch.stop)


class Case(unittest.TestCase):
    def setUp(self):
        memory_on(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        (self.state / memory_words.OWNER_CHAT).write_text(json.dumps({"chat_id": OWNER, "owner": "ou_owner"}))
        self.deploy = self.note("Covey 的专项话题改完、检查过就直接部署到主人本机现网，网页有改一起发布。",
                                title="Covey 专项话题改完检查过直接部署主人本机现网")
        self.peers = self.note("同事的机器全程只读、不改任何东西；同事数据不外传。", title="同事机器只读、同事数据不外传")
        self.shell = self.note("话题用 shell 写文档时别把带反引号的命令放进未加引号的 heredoc。")
        self.rule = memstore.add(self.state, kind="rule", scope="global", text="回复主人用中文，会话再长也不换成英文。",
                                 said="owner", by="owner")
        self.split = memstore.add_decision(self.state, scope="topic", owner_key="t" * 64,
                                           question=f"现网池子里的 {self.peers.code} 在 F3 里还要拆分吗？",
                                           answer=f"保持 {self.peers.code} 原文留在池子，不拆分（推荐）")
        self.codex = memstore.add_decision(self.state, scope="topic", owner_key="t" * 64,
                                           question="设备上的 codex 怎么处理？", answer="两台都装到同版，并推进快路（推荐）")
        self.short = memstore.add_decision(self.state, scope="topic", owner_key="t" * 64, question="现在合入吗？", answer="合")
        self.mine = memstore.add(self.state, kind="note", scope="topic", owner_key="t" * 64, text="这个话题的库只读。",
                                 said="owner", by="owner")

    def note(self, text, **kw):
        return memstore.add(self.state, kind="note", scope="global", text=text, said="owner", by="owner", **kw)

    def shown(self, text, **kw):
        return memory_words.shown(text, self.state, **kw)


class RewriteTest(Case):
    def test_a_code_that_only_points_inside_brackets_goes_with_them(self):
        d, x = self.split.code, self.codex.code
        self.assertEqual(self.shown(f"已开话题（按 `{x}`）。"), "已开话题。")
        self.assertEqual(self.shown(f"部署照旧（{d}）"), "部署照旧")
        self.assertEqual(self.shown(f"部署照旧 ({d})，接着做"), "部署照旧，接着做")
        self.assertEqual(self.shown(f"（见 {self.deploy.code}、{self.peers.code}）都照办"), "都照办")
        self.assertEqual(self.shown(f"接着做（按 `{x}`，带上测试）"), "接着做（带上测试）")
        self.assertEqual(self.shown(f"（{d}；另见 T951）"), "（另见 T951）")

    def test_the_real_samples_read_as_words(self):
        r, n, q, d = self.deploy.code, self.peers.code, self.shell.code, self.split.code
        self.assertEqual(self.shown(f"带池子记忆 `{r}`、`{n}`、`{q}`，派给 T12。"),
                         "带池子记忆「Covey 专项话题改完检查过直接部署主人本机现网」、「同事机器只读、同事数据不外传」、"
                         "「话题用 shell 写文档时别把带反引号的命令放进未加引号的…」，派给 T12。")
        self.assertEqual(self.shown(f"`{n}` 维持不拆（`{d}`）。"), "「同事机器只读、同事数据不外传」维持不拆。")
        self.assertEqual(self.shown(f"按 {r} 直接部署"), "按「Covey 专项话题改完检查过直接部署主人本机现网」直接部署")
        self.assertEqual(self.shown(f"带上 {r} 和 {self.rule.code}"),
                         "带上「Covey 专项话题改完检查过直接部署主人本机现网」和「回复主人用中文，会话再长也不换成英文」")

    def test_a_mixed_bracket_keeps_its_words_and_names_the_entry_it_stands_on(self):
        self.assertEqual(self.shown(f"（带上 {self.deploy.code}）"), "（带上「Covey 专项话题改完检查过直接部署主人本机现网」）")

    def test_a_title_written_beside_its_code_is_not_said_twice(self):
        r = self.deploy.code
        self.assertEqual(self.shown(f"见 {r}「Covey 专项话题」"), "见「Covey 专项话题」")
        self.assertEqual(self.shown(f"- {r} Covey 的专项话题改完、检查过就直接部署"), "- Covey 的专项话题改完、检查过就直接部署")
        self.assertEqual(self.shown(f"- {r}：Covey 专项话题改完检查过直接部署主人本机现网"),
                         "- Covey 专项话题改完检查过直接部署主人本机现网")
        self.assertEqual(self.shown(f"「{r}」照做"), "「Covey 专项话题改完检查过直接部署主人本机现网」照做")
        self.assertEqual(self.shown(f"| {r} | 照做 |"), "| 「Covey 专项话题改完检查过直接部署主人本机现网」 | 照做 |")

    def test_a_decision_reads_as_its_answer_and_is_never_a_link(self):
        d = self.split.code
        said = self.shown(f"按 {d} 部署", url=URL)
        self.assertEqual(said, "按「保持『同事机器只读、同事数据不外传』原文留在池子，不拆分」部署")
        self.assertNotIn("](", said)
        self.assertNotIn("主人答", said)
        self.assertNotIn("推荐", self.shown(f"按 {self.codex.code} 办", url=URL))
        self.assertEqual(self.shown(f"按 {self.codex.code} 办"), "按「两台都装到同版，并推进快路」办")
        self.assertEqual(self.shown(f"按 {self.short.code} 办"), "按「现在合入吗？→合」办")

    def test_a_rule_or_note_of_global_in_force_links_to_its_entry_on_the_page(self):
        r = self.deploy.code
        self.assertEqual(self.shown(f"按 {r} 部署", url=URL),
                         f"按[「Covey 专项话题改完检查过直接部署主人本机现网」]({URL}?code={r})部署")
        self.assertNotIn("](", self.shown(f"按 {self.mine.code} 办", url=URL))  # A topic's: the page may not have it.
        memstore.archive(self.state, self.peers.ref, who="owner")
        self.assertEqual(self.shown(f"按 {self.peers.code} 办", url=URL), "按「同事机器只读、同事数据不外传」办")

    def test_code_links_paths_commits_and_unknown_codes_stay_as_they_are(self):
        r = self.deploy.code
        for text in (f"```\nmemory_used: [\"{r}\"]\n按 {r}\n```", f"~~~json\n{{\"memory\": [\"{r}\"]}}\n~~~",
                     f"跑 `git log {r}` 看看", f"`{r} RABCD`", f"路径 /a/{r}/b", f"https://x.example/memory?code={r}",
                     f"[{r}](http://q.example)", f"![{r}](a.png)", f"commit a1{r}b", f"x.{r} 和 {r}.py", f"{r}-fix",
                     f"key={r}", "RESET 和 RABCD 不存在", f"\"{r}\"", f"ref:{r}", "T951 #8355 dc880c3"):
            self.assertEqual(self.shown(text), text, text)

    def test_a_fence_hides_its_codes_but_the_words_after_it_change(self):
        r = self.deploy.code
        self.assertEqual(self.shown(f"```\n{r}\n```\n按 {r}"),
                         f"```\n{r}\n```\n按「Covey 专项话题改完检查过直接部署主人本机现网」")

    def test_with_the_memory_off_nothing_changes(self):
        text = f"按 {self.deploy.code} 部署（{self.split.code}）"
        with unittest.mock.patch.dict("os.environ", {"COVEY_MEMORY": "0"}):
            self.assertEqual(self.shown(text), text)
            from covey.app import prompts
            self.assertNotIn(prompts.CODES_RULE, prompts.constraints("Claude Code"))

    def test_words_without_a_code_of_the_store_are_the_same_object(self):
        text = "没有短码的一句话，T12 和 `code`。"
        self.assertIs(self.shown(text), text)
        self.assertIs(memory_words.shown("按 R9ZZZ 办", None), "按 R9ZZZ 办")

    def test_a_changed_title_is_read_again_once_the_memory_changed(self):
        r = self.deploy.code
        self.assertIn("Covey 专项话题", self.shown(f"按 {r} 办"))
        memstore.edit(self.state, self.deploy.ref, title="改完就部署", who="owner")
        self.assertEqual(self.shown(f"按 {r} 办"), "按「改完就部署」办")


class WhereTest(Case):
    def test_only_the_owners_private_chat_has_its_codes_rewritten(self):
        text = f"按 {self.deploy.code} 部署"
        self.assertIn("「", memory_words.for_chat(text, self.state, OWNER))
        self.assertEqual(memory_words.for_chat(text, self.state, GROUP), text)
        self.assertEqual(memory_words.for_chat(text, self.state, ""), text)
        (self.state / memory_words.OWNER_CHAT).unlink()
        self.assertEqual(memory_words.for_chat(text, self.state, OWNER), text)

    def test_a_proxy_topics_turn_keeps_its_words(self):
        from covey.app.turn_delivery import TurnDelivery

        topic = self.state / "topics" / "p1"
        topic.mkdir(parents=True)
        (topic / "topic.json").write_text(json.dumps({"proxy": {"contact": "ou_x"}}))
        text = f"按 {self.deploy.code} 部署"
        delivery = SimpleNamespace(ctx=SimpleNamespace(state_dir=self.state, history_path=topic / "history.jsonl"),
                                   job=SimpleNamespace(chat_id=OWNER))
        self.assertEqual(TurnDelivery.owner_words(delivery, text), text)
        (topic / "topic.json").write_text("{}")
        self.assertIn("「Covey", TurnDelivery.owner_words(delivery, text))


class QuestionTest(Case):
    def record(self, chat=OWNER):
        d = self.split.code
        return {"token": "q1", "chat_id": chat, "topic_id": "t1", "questions": [
            {"id": "deploy", "header": f"部署（{d}）", "question": f"按 `{d}` 部署这一批吗？", "context": f"依据 {self.deploy.code}",
             "options": [{"label": f"部署 {d}", "description": f"按 {self.deploy.code} 直接部署"},
                         {"label": "先不", "description": "攒一批再问"}]}],
            "triage": {"state": "escalated", "why": f"{d} 只管上一批", "if_not": "晚一天"}, "mirror_output": f"上一轮按 {d}。"}

    def test_the_words_change_and_the_ids_and_labels_do_not(self):
        record = self.record()
        before = json.dumps(record, ensure_ascii=False)
        view = memory_words.question_view(record, self.state)
        self.assertEqual(json.dumps(record, ensure_ascii=False), before)  # The record itself is never changed.
        q, = view["questions"]
        self.assertEqual(q["id"], "deploy")
        self.assertEqual([o["label"] for o in q["options"]], [f"部署 {self.split.code}", "先不"])
        self.assertEqual(q["header"], "部署")
        self.assertNotIn(self.split.code, q["question"])
        self.assertNotIn(self.deploy.code, q["context"] + q["options"][0]["description"])
        self.assertNotIn("](", q["options"][0]["description"])  # Plain words: some fields are plain text.
        self.assertNotIn(self.split.code, view["triage"]["why"] + view["mirror_output"])
        self.assertEqual(view["triage"]["if_not"], "晚一天")

    def test_a_question_elsewhere_is_drawn_from_the_record_itself(self):
        record = self.record(GROUP)
        self.assertIs(memory_words.question_view(record, self.state), record)

    def test_the_card_drawn_from_the_view_answers_as_the_record_does(self):
        from covey.app import answers
        from covey.cards.question_cards import question_card

        record = {**self.record(), "status": "pending", "job_id": 1, "blocking": False}
        built = question_card(memory_words.question_view(record, self.state))
        card = json.dumps(built, ensure_ascii=False)
        self.assertNotIn(f"（{self.split.code}）", card)
        self.assertIn(f"部署 {self.split.code}", card)  # The option's label as the record has it.
        # The card sends the option's place (a button's ``pick``, a dropdown's value), never its words; the record's
        # questions read the place back as the label the record has.
        from covey.cards.question_cards import PICK

        sent = [b["behaviors"][0]["value"][PICK] for b in _elements(built) if b.get("tag") == "button"
                and b.get("behaviors") and isinstance(b["behaviors"][0].get("value"), dict) and PICK in b["behaviors"][0]["value"]]
        sent += [o["value"] for e in _elements(built) if e.get("tag") in ("select_static", "multi_select_static") for o in e["options"]]
        self.assertIn(0, [int(v) for v in sent if str(v).isdigit()])
        picked = answers.form_answers(record["questions"], {"option_0": "0"})
        self.assertIn(f"部署 {self.split.code}", json.dumps(picked, ensure_ascii=False))
        self.assertEqual(answers.answer_fields(record, {"deploy": f"部署 {self.split.code}"}, "").get("option_0"), "0")


class OutletTest(Case):
    def test_the_cards_words_are_rewritten_where_their_t_numbers_are_linked(self):
        from covey.app.relay_send import linked, owner_facts

        text = f"按 {self.split.code} 维持不拆（{self.codex.code}）"
        card = {"body": {"elements": [{"tag": "markdown", "content": text}]}}
        built, words = linked(self.state, OWNER, card, text)
        self.assertEqual(words, "按「保持『同事机器只读、同事数据不外传』原文留在池子，不拆分」维持不拆")
        self.assertEqual(built["body"]["elements"][0]["content"], words)
        self.assertEqual(linked(self.state, GROUP, None, text)[1], text)
        # In a card's Markdown a one-line fence is inline code (optimize_markdown): there inline code stays code; the words a
        # card is drawn from are rewritten before, fences and all.
        fenced = f"```\n{self.deploy.code}\n```\n（按 `{self.codex.code}`）"
        self.assertEqual(linked(self.state, OWNER, None, f"`{self.deploy.code}`")[1], f"`{self.deploy.code}`")
        self.assertEqual(owner_facts(self.state, OWNER, {"text": fenced, "n": 1}), {"text": f"```\n{self.deploy.code}\n```\n", "n": 1})
        self.assertEqual(owner_facts(self.state, GROUP, {"text": fenced}), {"text": fenced})

    def test_a_web_turn_and_a_question_item_read_as_words_and_keep_their_labels(self):
        from covey.app import web_collect

        turn = {"role": "agent", "body": f"按 {self.deploy.code} 部署"}
        self.assertIn("「Covey", web_collect.owner_turn(self.state, OWNER, turn)["body"])
        self.assertEqual(turn["body"], f"按 {self.deploy.code} 部署")  # Never changed in place.
        owner = {"role": "owner", "body": f"删掉 {self.deploy.code}"}
        self.assertIs(web_collect.owner_turn(self.state, OWNER, owner), owner)
        made = web_collect.owner_question(self.state, OWNER, {"questions": [
            {"id": "a", "header": "h", "question": f"按 {self.split.code} 吗", "context": "",
             "options": [{"label": f"选 {self.split.code}", "description": f"见 {self.deploy.code}"}]}]})
        q, = made["questions"]
        self.assertEqual((q["id"], q["options"][0]["label"]), ("a", f"选 {self.split.code}"))
        self.assertNotIn(self.split.code, q["question"])
        self.assertNotIn("](", q["options"][0]["description"])
        fields = web_collect.owner_progress(self.state, OWNER, {"goal": f"照 {self.deploy.code} 收尾",
                                                                 "milestones": [{"id": "n1", "text": f"按 {self.codex.code} 装好"}]})
        self.assertNotIn(self.deploy.code, fields["goal"])
        self.assertEqual(fields["milestones"][0]["id"], "n1")
        self.assertNotIn(self.codex.code, fields["milestones"][0]["text"])


class RulesTest(unittest.TestCase):
    def setUp(self):
        memory_on(self)

    def test_every_turn_is_told_to_say_a_rule_in_words_and_the_old_wordings_are_gone(self):
        from covey.app import prompts
        from covey.app.project_lead import COLLAB_RULES, TOOL_RULES

        for lead in (False, True):
            self.assertIn(prompts.CODES_RULE, prompts.constraints("Claude Code", lead=lead))
        for words in ("记忆短码", "池子", "挑带", "工具参数", "检查点"):
            self.assertIn(words, prompts.CODES_RULE)
        self.assertIn('memory_used 写短码，如 ["R7K2M"]，回复里不写', prompts.RULES_GUIDE)
        shared = "".join(prompts.SHARED_RULES)
        self.assertNotIn("和它的短码", shared)
        self.assertIn("不写短码", shared)
        self.assertIn("不写短码", "".join(prompts.AREA_OPENING))
        with unittest.mock.patch.object(prompts.knowledge, "enabled", return_value=True):
            told = "".join(prompts.knowledge_rules())
        self.assertNotIn("列出短码和正文", told)
        self.assertIn("不列短码", told)
        self.assertIn("不写记忆短码、条目和动作的编号", "".join(TOOL_RULES))
        self.assertIn("不说它的编号", "".join(COLLAB_RULES))


def _elements(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _elements(value)
    elif isinstance(node, list):
        for value in node:
            yield from _elements(value)

if __name__ == "__main__":
    unittest.main()
