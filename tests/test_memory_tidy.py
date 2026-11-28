"""整理记忆 (COVEY_MEMORY on, app/memory_tidy.py, core/project_memory.py settle and unsettle, cards/digest.py memory_elements,
app/digest.py memory_click): once a day, a project's 资料 and 决定 (memory.json's lines and decisions.jsonl's) put away by rules
alone (done, foreign, unused: only ``memory_used``, ``show`` and a question answered before count as a use, never being
carried) and put back by 撤销, never a 规矩 (the owner's nor the lead's), and the evening digest's part with its one button; no
model is asked.

ElsewhereTest is what went wrong once: a topic of one project asked the owner, on one card, how eight lines of another
project's memory should count from now on (a migration), the answers were noted as the first project's 当前决定, and that night's
整理 left six of them there and wrote two of the other project's rules into the first as its own."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import UNSET_OFF
from support import make_context

from covey.app import memory_tidy
from covey.cards.digest import MEMORY_KIND, digest_card, digest_text, memory_elements
from covey.core import memory, memory_usage, memstore, project_memory, projects
from covey.core.state import write_json

ON = {memory.ENV: "1", memory.LEARN_ENV: "1", projects.ENV: "1", projects.MEMORY_ENV: "1"}
PID, CHAT = "a1b2c3d4e5f6", "oc_owner_dm"
DAY = 86400
NOW = 1_800_000_000.0


def buttons(card):
    found = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("tag") == "button":
                found.append(node["behaviors"][0]["value"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    walk(card)
    return found


class Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {name: value for name, value in os.environ.items()
                                               if not name.startswith(("COVEY_", "LARK_BRIDGE_"))} | UNSET_OFF, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.dict(os.environ, ON)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ctx = make_context(Path(tmp.name))
        self.state = self.ctx.state_dir
        projects.create(self.state, projects.new_record(chat_id=CHAT, name="网关", goal="把网关发出去", project_id=PID, now=NOW))
        self.logs = []

    def line(self, text, source="owner_answer", kind="decision", at=NOW):
        entry, _ = project_memory.add(self.state, PID, text, kind, source, now=at)
        return entry["id"]

    def tidy(self):
        return memory_tidy.MemoryTidy(mock.Mock(log=self.logs.append))

    def parts(self):
        found = {}
        for row in project_memory.stored(self.state, PID):
            if not row["deleted"]:
                found[row["text"]] = memory.describe(row)
        return found


class SettleTest(Case):
    def test_a_settled_line_is_history_and_unsettle_puts_it_back(self):
        one = self.line("现在部署吗→部署")
        settled = project_memory.settle(self.state, PID, [{"id": one, "valid": "done"}, {"id": "nope", "valid": "done"}])
        self.assertEqual(settled, [(one, {})])
        self.assertEqual(self.parts()["现在部署吗→部署"]["layer"], memory.HISTORY)
        self.assertEqual(project_memory.unsettle(self.state, PID, settled), [one])
        self.assertEqual(self.parts()["现在部署吗→部署"]["part"], memory.DECISION)

    def test_superseded_keeps_who_replaced_it(self):
        old, new = self.line("用 A 方案"), self.line("改用 B 方案")
        project_memory.settle(self.state, PID, [{"id": old, "valid": "superseded", "superseded_by": new}])
        facts = self.parts()["用 A 方案"]
        self.assertEqual((facts["valid"], facts["superseded_by"]), ("superseded", new))


class RunTest(Case):
    def test_idle_lines_are_put_away_the_owners_rules_stay_and_what_went_is_in_the_log(self):
        a = self.line("主人答：镜像先发预发吗？→先发预发（推荐）", at=NOW - 40 * DAY)
        b = self.line("主人答：用哪个配置？→A", at=NOW - 40 * DAY)
        self.line("用中文回复", source="owner", kind="constraint", at=NOW - 40 * DAY)  # His 长期约定: never put away.
        run = self.tidy().run(self.ctx, CHAT, NOW, now=NOW)
        parts = self.parts()
        self.assertEqual(parts["主人答：镜像先发预发吗？→先发预发（推荐）"]["valid"], "done")
        self.assertEqual(parts["用中文回复"]["part"], memory.RULE)
        view = memory_tidy.view_of(run)
        self.assertEqual(view["away"], {"idle": 2, "done": 0, "foreign": 0})
        self.assertEqual(sorted(entry for entry, _, _ in run["projects"][0]["settled"]), sorted([a, b]))
        logged = [row for row in memstore.log_read(self.state) if row["action"] == "archive"]  # And what went away is in 记录.
        self.assertEqual(len(logged[0]["refs"]), 2)
        self.assertEqual(len(memstore.undo(self.state, logged[0]["undo_token"], now=NOW + 1)), 2)  # That line's own 撤销.
        self.assertEqual(self.parts()["主人答：镜像先发预发吗？→先发预发（推荐）"]["valid"], "active")

    def test_a_line_nobody_used_for_long_goes_without_the_model_and_one_used_stays(self):
        stale = self.line("主人答：旧问题→旧回答", at=NOW - 40 * DAY)
        used = self.line("主人答：常用的问题→常用的回答", at=NOW - 40 * DAY)
        memory_usage.note(self.state, [f"project:{PID}:{used}"], now=NOW - 2 * DAY)
        run = self.tidy().run(self.ctx, CHAT, NOW, now=NOW)
        self.assertEqual(self.parts()["主人答：旧问题→旧回答"]["valid"], "done")
        self.assertEqual(self.parts()["主人答：常用的问题→常用的回答"]["valid"], "active")
        self.assertEqual(memory_tidy.view_of(run)["away"]["idle"], 1)
        self.assertEqual(stale, run["projects"][0]["settled"][0][0])

    def test_only_memory_used_show_and_a_question_answered_before_count_as_a_use_never_being_carried(self):
        """T908 4.2: being carried whole every turn is no use (a line carried and never of use stayed for good); a checkpoint's
        ``memory_used``, ``covey memory show`` and a question it answered before (question_dedup) are."""
        carried = self.line("主人答：天天带的问题→天天带的回答", at=NOW - 40 * DAY)
        listed = self.line("主人答：只列标题的问题→只列标题的回答", at=NOW - 40 * DAY)
        confirmed = self.line("主人答：确认过的问题→确认过的回答", at=NOW - 40 * DAY)
        used = {kind: self.line(f"主人答：{kind}用到的问题→回答", at=NOW - 40 * DAY) for kind in ("dedup", "show", "memory_used")}
        with mock.patch("time.time", return_value=NOW - DAY):
            memory_usage.carried(self.state, [f"project:{PID}:{carried}"])
            memory_usage.listed(self.state, [f"project:{PID}:{listed}"])
            memory_usage.signal(self.state, [f"project:{PID}:{confirmed}"], "confirm")
            for kind, entry in used.items():
                memory_usage.signal(self.state, [f"project:{PID}:{entry}"], kind)
        self.tidy().run(self.ctx, CHAT, NOW, now=NOW)
        parts = self.parts()
        self.assertEqual(parts["主人答：天天带的问题→天天带的回答"]["valid"], "done")
        self.assertEqual(parts["主人答：只列标题的问题→只列标题的回答"]["valid"], "done")
        self.assertEqual(parts["主人答：确认过的问题→确认过的回答"]["valid"], "done")
        for kind in used:
            self.assertEqual(parts[f"主人答：{kind}用到的问题→回答"]["valid"], "active", kind)
        row = memory_usage.counts(self.state)[f"project:{PID}:{carried}"]
        self.assertEqual((row["e"], row["last"]), (1.0, 0.0))  # Counted for the ranking, not as a use.

    def test_decisions_jsonl_is_read_and_its_idle_decision_goes_and_comes_back(self):
        """T908: 整理记忆 read only memory.json, so not one of the answers noted since (decisions.jsonl) was ever put in order."""
        old = memstore.add_decision(self.state, scope="project", owner_key=PID, question="镜像先发预发吗？", answer="先发预发",
                                    topic_key="ab" * 16, question_ref="0123456789abcdef-1", now=NOW - 40 * DAY)
        fresh = memstore.add_decision(self.state, scope="project", owner_key=PID, question="超时设多少？", answer="30 秒",
                                      topic_key="ab" * 16, question_ref="0123456789abcdef-2", now=NOW - DAY)
        tidy = self.tidy()
        run = tidy.run(self.ctx, CHAT, NOW, now=NOW)
        self.assertEqual(run["projects"][0]["decided"], [[old.ref, "idle"]])
        self.assertEqual(memstore.find(self.state, old.ref).status, "done")
        self.assertEqual(memstore.find(self.state, fresh.ref).status, "active")
        self.assertEqual(memory_tidy.view_of(run)["away"], {"idle": 1, "done": 0, "foreign": 0})
        tidy.decide(self.ctx, run["id"], "undo")
        self.assertEqual(memstore.find(self.state, old.ref).status, "active")

    def test_no_rule_is_ever_put_away_the_owners_nor_the_leads(self):
        owner = memstore.add(self.state, kind="rule", scope="project", owner_key=PID, text="合入前先跑全量", now=NOW - 400 * DAY)
        lead = memstore.add(self.state, kind="rule", scope="project", owner_key=PID, text="不碰同事的机器", said="summary",
                            by="lead", now=NOW - 400 * DAY)
        older = self.line("改共享配置前先问负责人", source="lead", kind="constraint", at=NOW - 400 * DAY)
        memstore.add(self.state, kind="note", scope="project", owner_key=PID, text="预发地址在简报里", said="summary", by="lead",
                     now=NOW - 400 * DAY)
        run = self.tidy().run(self.ctx, CHAT, NOW, now=NOW)
        self.assertEqual(memory_tidy.view_of(run)["away"]["idle"], 1)  # The note alone.
        self.assertEqual([memstore.find(self.state, entry.ref).status for entry in (owner, lead)], ["active", "active"])
        kinds = {entry.ref: entry.kind for entry in memstore.entries(self.state, scope="project", owner_key=PID, statuses=None)}
        self.assertEqual(kinds[f"project:{PID}:{older}"], "note")  # An older lead's limit no turn carried: read as a 资料,
        self.assertEqual(memstore.find(self.state, f"project:{PID}:{older}").status, "active")  # and still never put away.

    def test_it_runs_with_the_memory_alone_once_a_day_and_not_with_the_memory_off(self):
        """T908 4.2: it ran only with 记忆自己长 and the 早晚汇总 both on; now with COVEY_MEMORY, its own worker once a day."""
        self.line("主人答：旧问题→旧回答", at=NOW - 40 * DAY)
        tidy = self.tidy()
        with mock.patch.dict(os.environ, {memory.LEARN_ENV: "0"}):
            self.assertEqual([name for name, _ in tidy.threads()], ["memory-tidy"])
            first = tidy.tick(self.ctx, now=NOW)
            self.assertEqual(memory_tidy.view_of(first)["away"]["idle"], 1)
            self.assertEqual(tidy.tick(self.ctx, now=NOW + 3600)["id"], first["id"])  # The same day: the same run.
            self.assertNotEqual(tidy.tick(self.ctx, now=NOW + DAY)["id"], first["id"])
        with mock.patch.dict(os.environ, {memory.ENV: "0"}):
            self.assertEqual(tidy.threads(), ())
            self.assertEqual(tidy.tick(self.ctx, now=NOW + 2 * DAY), {})

    def test_a_slot_is_tidied_once(self):
        self.line("主人答：旧问题→旧回答", at=NOW - 40 * DAY)
        tidy = self.tidy()
        self.assertEqual(tidy.view(self.ctx, CHAT, NOW, now=NOW)["away"]["idle"], 1)
        self.line("主人答：另一个旧问题→回答", at=NOW - 40 * DAY)
        self.assertEqual(tidy.view(self.ctx, CHAT, NOW, now=NOW)["away"]["idle"], 1)  # Not tidied again: the run is kept.
        self.assertEqual(len(memory_tidy.load(self.state)["runs"]), 1)

    def test_undo_puts_back_what_it_put_away_once_and_the_retired_buttons_decide_nothing(self):
        self.line("主人答：旧问题→旧回答", at=NOW - 40 * DAY)
        tidy = self.tidy()
        run = tidy.run(self.ctx, CHAT, NOW, now=NOW)
        self.assertEqual(self.parts()["主人答：旧问题→旧回答"]["valid"], "done")
        for op in ("confirm", "wrong"):  # 确认长期 and 不对 of an older card: the run as it is.
            self.assertFalse(tidy.decide(self.ctx, run["id"], op, now=NOW).get("undone"))
        self.assertEqual(self.parts()["主人答：旧问题→旧回答"]["valid"], "done")
        undone = tidy.decide(self.ctx, run["id"], "undo")
        self.assertTrue(undone["undone"])
        self.assertEqual(self.parts()["主人答：旧问题→旧回答"]["valid"], "active")
        self.assertIsNone(tidy.decide(self.ctx, "tdeadbeef", "undo"))

    def test_a_record_an_older_release_wrote_is_read_and_its_new_rules_and_cursors_are_ignored(self):
        write_json(memory_tidy.path(self.state), {"schema_version": 1, "cursors": {PID: 40}, "runs": [
            {"id": "t0a1b2c3d", "slot": NOW, "chat_id": CHAT, "at": NOW, "expired": [], "projects": [
                {"id": PID, "name": "网关", "settled": [["e1", {}, "superseded"], ["e2", {}, "idle"]],
                 "rules": [{"entry": "e3", "text": "发网关镜像都先发预发", "state": "open"}]}]}]})
        run = memory_tidy.load(self.state)["runs"][0]
        view = memory_tidy.view_of(run)
        self.assertEqual(view["away"], {"idle": 1, "done": 1, "foreign": 0})  # A line the model called superseded counts as done.
        self.assertNotIn("rules", view)
        self.assertNotIn("cursors", memory_tidy.load(self.state))



OTHER = "f6e5d4c3b2a1"
CARD = "0f0e0d0c0b0a0908"  # The migration card's question record.


class ElsewhereTest(Case):
    """A topic of this project (网关) sorted out the memory of another project (回放, ``OTHER``) on one question card."""

    BASELINE = "回放口径（主人定）：每次回放另抽新的 50 个请求，新旧两版一起跑，抖动导致的差也算差，不拿抖动解释变差的请求。"
    SERVICE = "评估时不能把“挪给下游”直接算作合理的减少，要核实下游真的接住了，并说清每个指标的含义和判定方法。"

    def setUp(self):
        super().setUp()
        projects.create(self.state, projects.new_record(chat_id=CHAT, name="回放", goal="回放工具上线", project_id=OTHER,
                                                        now=NOW))
        self.theirs = [project_memory.add(self.state, OTHER, text, "decision", "lead", now=NOW - 3 * DAY)[0]["id"]
                       for text in (self.BASELINE, self.SERVICE)]

    def migration(self, token=CARD):
        """The card as an older release noted it: a 当前决定 for each of 回放's lines, 长期都照这条做, each then carried out on
        its line by a correction whose origin names the question (as the migration's script wrote it)."""
        lines = [{"index": i, "item": f"memory-{entry}",
                  "text": f"主人答：回放专项的记忆〔{entry}〕「{text}」这条以后怎么算？→长期都照这条做（推荐）"}
                 for i, (entry, text) in enumerate(zip(self.theirs, (self.BASELINE, self.SERVICE)), 1)]
        project_memory.note_answers(self.state, PID, {"token": token, "topic_id": "ab" * 16}, lines, now=NOW - 2 * DAY)
        for entry in self.theirs:
            project_memory.fix(self.state, OTHER, entry, "confirm", now=NOW - 2 * DAY,
                               origin={"message_id": "om_card", "question": token, "item": f"memory-{entry}"})
        return [project_memory.answer_id(token, i) for i in (1, 2)]

    def facts(self, entry):
        return memory.describe(next(row for row in project_memory.stored(self.state, PID) if row["id"] == entry))

    def test_a_card_that_sorted_out_another_projects_memory_goes_by_rules(self):
        card = self.migration()
        for text in ("主人答：网关先发预发吗？→先发预发（推荐）", "主人答：这次网关也先发预发吗？→先发预发", "主人答：网关超时设多少？→30 秒"):
            self.line(text)
        tidy = self.tidy()
        run = tidy.run(self.ctx, CHAT, NOW, now=NOW)
        self.assertEqual([(self.facts(entry)["valid"], self.facts(entry)["layer"]) for entry in card], [("done", memory.HISTORY)] * 2)
        self.assertEqual(run["projects"][0]["settled"], [[card[0], {}, "foreign"], [card[1], {}, "foreign"]])
        self.assertEqual(memory_tidy.view_of(run)["away"], {"idle": 0, "done": 0, "foreign": 2})
        tidy.decide(self.ctx, run["id"], "undo")
        self.assertEqual([self.facts(entry)["part"] for entry in card], [memory.DECISION] * 2)

    def test_an_answer_carried_out_on_another_line_goes_as_done_and_one_that_corrected_itself_stays(self):
        mine = self.line("网关用 A 方案部署", source="lead")
        token = "0123456789abcdef"
        project_memory.note_answers(self.state, PID, {"token": token}, [
            {"index": 1, "item": "keep", "text": "主人答：网关用 A 方案那条以后怎么算？→长期都照这条做"},
            {"index": 2, "item": "scope", "text": "主人答：超时只管预发吗？→只管预发"}], now=NOW - DAY)
        project_memory.fix(self.state, PID, mine, "confirm", origin={"message_id": "om_card", "question": token, "item": "keep"},
                           now=NOW)
        second = project_memory.answer_id(token, 2)
        project_memory.fix(self.state, PID, second, "scope", origin={"message_id": "om_card", "question": token, "index": 2},
                           applies="只在预发", now=NOW)
        run = self.tidy().run(self.ctx, CHAT, NOW, now=NOW)
        self.assertEqual(run["projects"][0]["settled"], [[project_memory.answer_id(token, 1), {}, "done"]])
        self.assertEqual(self.facts(second)["part"], memory.DECISION)  # Its own correction holds nothing elsewhere.
        self.assertEqual(self.facts(mine)["part"], memory.RULE)

class CardTest(Case):
    VIEW = {"run": "t0a1b2c3d", "away": {"idle": 1, "done": 8}, "undone": False}

    def test_the_part_says_what_went_away_and_has_its_one_button(self):
        elements = memory_elements({"memory_tidy": self.VIEW})
        text = json.dumps(elements, ensure_ascii=False)
        self.assertIn("收起 9 条：办完的 8 条、很久没用的 1 条", text)
        self.assertNotIn("候选", text)  # The evening candidates are gone.
        self.assertIn("在网页记录里能放回", text)
        self.assertNotIn("新约定", text)
        self.assertEqual([value["op"] for value in buttons(elements)], ["undo"])
        self.assertTrue(all(value["kind"] == MEMORY_KIND and value["run"] == "t0a1b2c3d" for value in buttons(elements)))
        elsewhere = {**self.VIEW, "away": {"idle": 0, "done": 2, "foreign": 6}}
        self.assertIn("收起 8 条：办完的 2 条、别的专项的 6 条", json.dumps(memory_elements({"memory_tidy": elsewhere}), ensure_ascii=False))

    def test_decided_and_undone_are_grey_lines_and_none_without_the_part(self):
        view = {**self.VIEW, "undone": True}
        text = json.dumps(memory_elements({"memory_tidy": view}), ensure_ascii=False)
        self.assertIn("已撤销，都放回去了", text)
        # A view an older release saved (rules with 确认长期 and 不对) is drawn without them.
        old = {**self.VIEW, "rules": [{"project": PID, "name": "网关", "entry": "e1", "text": "发网关镜像都先发预发", "state": "open"}]}
        self.assertEqual([value["op"] for value in buttons(memory_elements({"memory_tidy": old}))], ["undo"])
        self.assertEqual(buttons(memory_elements({"memory_tidy": view})), [])
        self.assertEqual(memory_elements({}), [])

    def test_the_digest_card_and_its_markdown_carry_it(self):
        view = {"slot": NOW, "since": NOW - DAY, "waiting": [], "running": [], "done": [], "memory_tidy": self.VIEW}
        self.assertIn("整理记忆", json.dumps(digest_card(view), ensure_ascii=False))
        self.assertIn("收起 9 条", digest_text(view))


class ClickTest(Case):
    def test_the_owners_click_is_decided_after_the_ack_and_others_are_ignored(self):
        from covey.app.digest import Digest
        digest = Digest(mock.Mock(log=self.logs.append), mock.Mock(), mock.Mock())
        digest.memory_tidy = mock.Mock()
        event = {"message_id": "om_card"}
        with mock.patch("covey.app.digest.after_ack", side_effect=lambda work: work()), \
                mock.patch.object(digest, "memory_decided") as decided:
            self.assertEqual(digest.claim(self.ctx, event, {"kind": MEMORY_KIND, "run": "t0a1b2c3d", "op": "undo"}, True),
                             "digest_memory")
            self.assertEqual(digest.claim(self.ctx, event, {"kind": MEMORY_KIND, "run": "bad", "op": "undo"}, True),
                             "digest_memory_ignored")
            self.assertEqual(digest.claim(self.ctx, event, {"kind": MEMORY_KIND, "run": "t0a1b2c3d", "op": "undo"}, False),
                             "owner_only")
        decided.assert_called_once_with(self.ctx, "om_card", "t0a1b2c3d", "undo")

    def test_an_older_cards_确认长期_and_不对_say_they_are_gone_and_decide_nothing(self):
        from covey.app.digest import Digest
        digest = Digest(mock.Mock(log=self.logs.append), mock.Mock(), mock.Mock())
        digest.memory_tidy = mock.Mock()
        event = {"message_id": "om_card", "chat_id": CHAT, "event_id": "ev1"}
        for op in ("confirm", "wrong"):
            with mock.patch("covey.app.digest.after_ack", side_effect=lambda work: work()), \
                    mock.patch.object(digest, "memory_decided") as decided:
                self.assertEqual(digest.claim(self.ctx, event, {"kind": MEMORY_KIND, "run": "t0a1b2c3d", "op": op, "project": PID,
                                                                "entry": "e1"}, True), "digest_memory_retired")
            decided.assert_not_called()
            digest.messenger.reply.assert_called()
        digest.memory_tidy.decide.assert_not_called()


if __name__ == "__main__":
    unittest.main()
