"""A project's lead on a Harness of its own (T700): what a new lead runs on (app/lead_harness.py binding: the butler's call, the
设置 card's 专项负责人, else the butler's), the 专项卡's 换配置 (app/harness_switch.py with the project named: only models a lead
can run on, no workspace rows, a reply to the 专项卡), the card's input box after it (app/project_facts.py), and the web page's
side (data contract v36: the lead's ``config``, its ``switch``, a failed lead turn's 换个模型, the models' ``bridge_tools``)."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support.fake_lark import FakeCardKit
from test_dispatch_cards import CHAT
from test_project_upgrade import UpCase

from covey.app import harness_switch as hs
from covey.app import lead_harness, project_facts, web_switch, web_turns
from covey.app.web_ops import Ops
from covey.cards import switch_form
from covey.core import butler, butler_settings, projects
from covey.core.harness import HarnessCapabilities
from covey.core.jobs import job_path, upsert_job_record
from covey.core.roster import CORE_HARNESSES
from covey.core.state import write_json
from covey.core.topics import load_topic, routed_topic, topic_file


def without_tools(module="covey.backends.dsh.bridge.DshBackend"):
    """The Harness ``module`` names as one that cannot take the Bridge's tools (every core Harness can since T700, so a
    test of one that cannot makes one)."""
    return mock.patch(module + ".capabilities", lambda self, ctx: HarnessCapabilities(rate_limit_retry=True))


def app(name, *, tools=True):
    backend = SimpleNamespace(capabilities=lambda ctx: HarnessCapabilities(bridge_tools=tools))
    return SimpleNamespace(backend=backend, harness=SimpleNamespace(backend=backend, context=SimpleNamespace(device=None)))


class BindingTest(unittest.TestCase):
    """Which Harness, model and budget a new lead takes, by rule alone."""

    def setUp(self):
        self.apps = {"claude": app("claude"), "codex": app("codex"), "dsh": app("dsh", tools=False)}
        self.root = {"harness_id": "claude", "model": "opus-x", "effort": "max"}

    def test_the_calls_choice_then_the_settings_then_the_butlers(self):
        call = {"harness": "codex", "model": "gpt-x", "effort": "high"}
        settings = butler_settings.LeadModel(harness="claude", model="sonnet-x", effort="low")
        self.assertEqual(lead_harness.binding(self.apps, self.root, call, settings),
                         {"harness_id": "codex", "model": "gpt-x", "effort": "high", "source": "call"})
        self.assertEqual(lead_harness.binding(self.apps, self.root, {}, settings),
                         {"harness_id": "claude", "model": "sonnet-x", "effort": "low", "source": "settings"})
        self.assertEqual(lead_harness.binding(self.apps, self.root, None, butler_settings.LeadModel()),
                         {"harness_id": "claude", "model": "opus-x", "effort": "max", "source": "butler"})

    def test_a_harness_that_cannot_run_a_lead_here_gives_way(self):
        for name in ("dsh", "agy"):  # No Bridge tools; not enabled.
            with self.subTest(name=name):
                found = lead_harness.binding(self.apps, self.root, None, butler_settings.LeadModel(harness=name))
                self.assertEqual((found["harness_id"], found["source"]), ("claude", "butler"))
        # 敏感词脱敏 is retired (T947): a butler topic.json that still has the flag does not narrow the lead's Harness.
        found = lead_harness.binding(self.apps, {**self.root, "redaction": True}, {"harness": "codex"}, None)
        self.assertEqual((found["harness_id"], found["source"]), ("codex", "call"))

    def test_why_a_harness_cannot_run_a_lead(self):
        self.assertEqual(lead_harness.refusal(self.apps, "codex"), "")
        self.assertEqual(lead_harness.refusal(self.apps, "dsh"), lead_harness.CANNOT_LEAD)
        self.assertIn("没有启用 agy", lead_harness.refusal(self.apps, "agy"))

    def test_the_menus_models_a_lead_can_run_on(self):
        menu = {"claude:a": {"bridge_tools": True}, "codex:b": {"bridge_tools": True}, "dsh:c": {}}
        self.assertEqual(list(lead_harness.lead_choices(menu)), ["claude:a", "codex:b"])
        self.assertFalse(lead_harness.can_lead(None))

    def test_the_saved_choice_is_read_from_either_shape(self):
        self.assertEqual(lead_harness.wanted_of(butler_settings.LeadModel(harness=" codex ", model="m")),
                         {"harness": "codex", "model": "m", "effort": ""})
        self.assertEqual(lead_harness.wanted_of({"harness": "", "model": "m"}), {})
        self.assertEqual(lead_harness.wanted_of("codex"), {})


class SettingsFileTest(unittest.TestCase):
    def test_the_leads_model_is_saved_and_read_back_and_a_bad_field_reads_as_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            butler_settings.update(tmp, lead=butler_settings.LeadModel(harness="codex", model="gpt-x", effort="high"))
            self.assertEqual(json.loads((Path(tmp) / butler_settings.FILE).read_text())["lead"],
                             {"harness": "codex", "model": "gpt-x", "effort": "high"})
            self.assertEqual(butler_settings.load(tmp).lead.harness, "codex")
        self.assertEqual(butler_settings.from_json({"lead": "codex"}).lead, butler_settings.LeadModel())


class StartTest(UpCase):
    """A project opened directly (topic_start with ``project``): its lead's Harness."""

    def lead_of(self, answer):
        self.assertTrue(answer["ok"], answer)
        project = projects.read_record(self.state, answer["data"]["project_id"])
        return project, load_topic(self.state, project["lead_topic_id"])

    def test_the_settings_lead_model_makes_the_new_lead(self):
        butler_settings.update(self.state, lead=butler_settings.LeadModel(harness="codex", model="codex-model", effort="high"))
        project, lead = self.lead_of(self.start("p1", project=True, title="迁移订单库", prompt="把订单库迁到新集群"))
        self.assertEqual((lead["harness_id"], lead["model"], lead["effort"]), ("codex", "codex-model", "high"))
        self.assertEqual((lead["workspace_name"], lead["binding_revision"]), ("project", 1))  # Where it reads: unchanged.
        self.assertNotIn("lead", project["origin"])
        self.assertTrue(any("harness=codex from=settings" in line for line in self.logs), self.logs[-5:])

    def test_a_harness_the_call_names_is_the_leads_and_one_that_cannot_lead_opens_nothing(self):
        butler_settings.update(self.state, lead=butler_settings.LeadModel(harness="agy"))
        project, lead = self.lead_of(self.start("p1", project=True, title="迁移订单库", prompt="x", harness="codex"))
        self.assertEqual(lead["harness_id"], "codex")
        self.assertEqual(project["origin"]["lead"]["harness"], "codex")
        with without_tools():
            refused = self.start("p2", project=True, title="另一个", prompt="x", harness="dsh")
        self.assertFalse(refused["ok"])
        self.assertIn(lead_harness.CANNOT_LEAD, refused["text"])
        self.assertEqual(len(projects.records(self.state)), 1)

    def test_none_chosen_or_one_not_enabled_is_the_butlers_as_before(self):
        butler_settings.update(self.state, lead=butler_settings.LeadModel(harness="gone", model="x"))
        _, lead = self.lead_of(self.start("p1", project=True, title="迁移订单库", prompt="x"))
        self.assertEqual((lead["harness_id"], lead["model"], lead["effort"]), ("claude", "claude-opus-x", "medium"))


class CardSwitchTest(UpCase):
    """换配置 on the 专项卡: the lead's Harness, model and budget."""

    def setUp(self):
        super().setUp()
        answer = self.start("p1", project=True, title="迁移订单库", prompt="把订单库迁到新集群")
        self.assertTrue(answer["ok"], answer)
        self.pid = answer["data"]["project_id"]
        self.project = projects.read_record(self.state, self.pid)
        self.lead = self.project["lead_topic_id"]
        self.card = projects.card_message(self.project)
        self.switch = self.app.harness_switch
        self.owner = self.app.runtime.owner_open_id
        self.sent.reset_mock()

    def topic(self):
        return load_topic(self.state, self.lead)

    def open(self, operator=None, click="click-open", **raw):
        value = {"kind": hs.OPEN, "topic_id": self.lead, "project_id": self.pid, **raw}
        event = {"chat_id": CHAT, "event_id": click, "message_id": self.card,
                 "operator_id": self.owner if operator is None else operator}
        with FakeCardKit({"data": {"card_id": "card_sw"}}, sent=("om_switch", False)):
            self.switch.open(self.ctx, event, value)
        forms = sorted((self.state / "switch-forms").glob("*.json"), key=lambda p: p.stat().st_mtime)
        return (json.loads(forms[-1].read_text()), forms[-1]) if forms else (None, None)

    def click(self, form, kind, option=None, tag=""):
        event = {"chat_id": CHAT, "event_id": "ev-" + kind + str(option) + tag, "message_id": form["message_id"],
                 "action_value": {"kind": kind, "token": form["token"]}, "option": option}
        with mock.patch.object(self.switch, "submit", side_effect=lambda busy, handler, *args: handler(*args) or True), \
                FakeCardKit({"data": {}}):
            self.switch.dispatch(self.ctx, event)

    def test_the_card_lists_only_models_a_lead_can_run_on_and_no_workspace(self):
        with without_tools():  # DeepSeek Harness as one without the Bridge's tools.
            form, _ = self.open()
        # The core's (an enabled extension's that takes the Bridge's tools, covey_internal's MASO, is listed too).
        self.assertEqual({key for key in form["models"] if key.partition(":")[0] in CORE_HARNESSES},
                         {"claude:claude-model", "codex:codex-model", "agy:agy-model"})
        self.assertNotIn("dsh:dsh-model", form["models"])
        self.assertEqual(form["lead"], {"project_id": self.pid, "name": "迁移订单库"})
        self.assertEqual(form["current"]["workspace"], "project")
        card = json.dumps(switch_form.build_switch_card(form), ensure_ascii=False)
        self.assertIn("专项「迁移订单库」的负责人", card)
        self.assertIn(switch_form.LEAD_GIST, card)
        labels = [setting.label for setting in switch_form.switch_settings(form)]
        self.assertNotIn("工作区", labels)
        self.assertNotIn("运行方式", labels)
        # A reply to the 专项卡 at the chat's top level, and a quote of it is the lead's.
        (profile, chat, built, key), kwargs = self.sent.call_args
        self.assertEqual((chat, kwargs.get("reply_to_message_id")), (CHAT, self.card))
        self.assertEqual(routed_topic(self.state, CHAT, form["message_id"]), self.lead)

    def test_confirm_rebinds_the_lead_and_the_project_card_shows_it(self):
        form, path = self.open()
        self.click(form, hs.MODEL, "codex:codex-model")
        self.click(json.loads(path.read_text()), hs.CONFIRM)
        done = json.loads(path.read_text())
        self.assertEqual(done["status"], "done", done.get("notice"))
        topic = self.topic()
        self.assertEqual((topic["harness_id"], topic["model"], topic["binding_revision"]), ("codex", "codex-model", 2))
        self.assertEqual((topic["project"]["role"], topic["workspace_name"]), ("lead", "project"))
        self.assertEqual([s["to"]["harness_id"] for s in topic["switches"]], ["codex"])
        self.assertTrue(projects.is_lead_topic(self.state, self.lead, CHAT))
        words = hs.done_text(done, done["models"]["codex:codex-model"], topic["effort"], "", None,
                             (done["switched"]["from"], done["switched"]["to"]))
        self.assertIn("专项「迁移订单库」的负责人", words)
        self.assertIn(hs.LEAD_NEXT, words)
        self.assertNotIn("工作区", words)
        # The lead's next wake-up goes to its new Harness (app/project_loop.py lead_app reads its topic.json).
        from covey.app import project_loop
        self.assertIs(project_loop.lead_app(self.env, self.state, topic), self.app.apps["codex"])
        # The 专项卡's input box: the new binding, though the lead's newest job ran on the old one.
        upsert_job_record(self.state, "lead-old", topic_id=self.lead, chat_id=CHAT, job_id=7, status="done",
                          harness_id="claude", model="claude-opus-x", effort="medium", binding_revision=1)
        facts = project_facts.facts(self.state, projects.read_record(self.state, self.pid), 1_900_000_000.0,
                                    titles={"codex": "Codex", "claude": "Claude Code"})
        self.assertEqual(facts.inputs[:2], ("Codex", "codex-model"))
        upsert_job_record(self.state, "lead-new", topic_id=self.lead, chat_id=CHAT, job_id=8, status="done",
                          harness_id="codex", model="codex-model", effort="high", binding_revision=2)
        facts = project_facts.facts(self.state, projects.read_record(self.state, self.pid), 1_900_000_000.0,
                                    titles={"codex": "Codex", "claude": "Claude Code"})
        self.assertEqual(facts.inputs, ("Codex", "codex-model", "思考 high"))

    def test_a_busy_lead_is_waited_for_and_only_the_owner_opens_it(self):
        form, path = self.open()
        self.click(form, hs.MODEL, "agy:agy-model")
        upsert_job_record(self.state, "lead-busy", topic_id=self.lead, chat_id=CHAT, job_id=9, status="running",
                          harness_id="claude")
        self.click(json.loads(path.read_text()), hs.CONFIRM)
        refused = json.loads(path.read_text())
        self.assertEqual(refused["status"], "ready")
        self.assertIn("负责人还有一轮在排队或在做，等它做完再确认更换", refused["notice"])
        self.assertEqual(self.topic()["harness_id"], "claude")
        job_path(self.state, "lead-busy").unlink()
        self.sent.reset_mock()
        self.assertEqual(self.open(operator="ou_someone_else", click="click-other")[0]["token"], form["token"])  # No new form.
        self.assertFalse(self.sent.called)

    def test_cancel_and_a_lead_switched_elsewhere_meanwhile_are_worded_for_the_lead(self):
        form, path = self.open()
        self.click(form, hs.MODEL, "codex:codex-model")
        topic = self.topic()  # Switched elsewhere (the web page) since the card was drawn.
        write_json(topic_file(self.state, self.lead), {**topic, "binding_revision": 5}, ensure_ascii=False, indent=2)
        self.click(json.loads(path.read_text()), hs.CONFIRM)
        refused = json.loads(path.read_text())
        self.assertIn("请在专项卡上重新点「换配置」", refused["notice"])
        self.assertNotIn("任务卡", refused["notice"])
        with mock.patch.object(hs, "notice_card", wraps=hs.notice_card) as drawn:
            self.click(refused, hs.CANCEL)
        self.assertEqual(json.loads(path.read_text())["status"], "cancelled")
        self.assertTrue(drawn.call_args.args[0].startswith("已取消换配置，负责人继续使用 "), drawn.call_args.args[0])

    def test_a_card_that_names_another_project_or_topic_opens_nothing(self):
        for raw in ({"project_id": "f" * 12}, {"topic_id": "e" * 32}):
            with self.subTest(raw=raw):
                self.assertEqual(self.open(click="click-" + json.dumps(raw), **raw), (None, None))
        with mock.patch.dict(os.environ, {projects.ENV: "0"}):
            self.assertEqual(self.open(click="click-off"), (None, None))

    def test_the_web_pages_switch_changes_the_lead_and_keeps_its_workspace(self):
        from covey.app import web_launch
        ops = Ops(self.env)
        models = {model["key"]: model for model in
                  web_launch.catalog(self.env, self.app.apps["claude"].harness.context, CHAT)["models"]}
        config = {**web_switch.topic_config(self.topic()), "model": "claude:claude-model",
                  "effort": models["claude:claude-model"]["start_budget"]}
        replies = []
        for each in self.app.topics.apps.values():
            each.messenger.reply = mock.Mock(side_effect=lambda *a, **k: replies.append((a[0], a[2])))

        def run(op_id, **payload):
            with mock.patch.object(self.app.runtime.access, "owner_chat", return_value=CHAT), \
                    mock.patch.object(self.app.topics.runtime.jobs, "in_topic", return_value=False):
                return ops.run({"kind": "switch", "op_id": op_id, "topic_key": self.lead, "payload": payload})
        moved = {**config, "workspace": "docs"}
        self.assertEqual(run("op-w1", **moved), ("failed", web_switch.LEAD_PLACE))
        # A lead's repositories are the project's, read only (T870): a payload that changes them is refused.
        self.assertEqual(run("op-w0", **{**config, "repos": [{"name": "covey", "mode": "write"}]}), ("failed", web_switch.LEAD_REPOS))
        with without_tools():
            self.assertEqual(run("op-w2", **{**config, "model": "dsh:dsh-model", "effort": models["dsh:dsh-model"]["start_budget"]}),
                             ("failed", lead_harness.CANNOT_LEAD))
        state, words = run("op-w3", **{**config, "model": "codex:codex-model",
                                       "effort": models["codex:codex-model"]["start_budget"]})
        self.assertEqual(state, "done", words)
        self.assertTrue(words.endswith("负责人下一轮起由新会话接着做"), words)
        self.assertEqual((self.topic()["harness_id"], self.topic()["workspace_name"]), ("codex", "project"))
        (target, text), = replies
        self.assertEqual(target.reply_to_message_id, self.card)  # Said in Feishu as a reply to the 专项卡.
        self.assertIn("专项「迁移订单库」的负责人", text)
        # The butler's model stays on the 设置 card.
        with mock.patch.object(self.app.runtime.access, "owner_chat", return_value=CHAT):
            refused = ops.run({"kind": "switch", "op_id": "op-w4", "topic_key": butler.butler_topic_id(CHAT), "payload": config})
        self.assertEqual(refused, ("failed", "管家的配置在设置的「配置」里改"))

    def test_a_failed_lead_turns_other_model_is_the_pages_switch(self):
        record = {"topic_id": self.lead, "chat_id": CHAT, "status": "failed", "execution_status": "failed",
                  "failure_code": "quota_exhausted", "harness_id": "claude", "binding_revision": 1, "job_id": 3}
        self.assertEqual(web_turns.failure_of(record)["how"], {"switch_model": web_turns.SWITCH})
        # The butler's is still Feishu's: its model is on the 设置 card.
        butler_turn = {**record, "topic_id": butler.butler_topic_id(CHAT)}
        self.assertEqual(web_turns.failure_of(butler_turn)["how"], {"switch_model": web_turns.FEISHU})


class CatalogTest(UpCase):
    def test_each_model_on_the_page_says_whether_a_lead_can_run_on_it(self):
        from covey.app import web_launch
        base = self.app.apps["claude"].harness.context
        found = {model["key"]: model["bridge_tools"] for model in web_launch.catalog(self.env, base, CHAT)["models"]}
        # Every core Harness takes the Bridge's tools (DeepSeek Harness since T700's second round).
        core = {"claude:claude-model": True, "codex:codex-model": True, "agy:agy-model": True,
                "dsh:dsh-model": True}
        # An enabled extension adds its own Harnesses' models (covey_internal: MASO's, no Bridge tools): only the core's are named.
        self.assertEqual({key: found.get(key) for key in core}, core)
        self.assertTrue(all(isinstance(value, bool) for value in found.values()), found)


if __name__ == "__main__":
    unittest.main()
