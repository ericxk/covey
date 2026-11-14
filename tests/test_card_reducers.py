"""The launch card's and 换配置's click reducers, pinned as they are. Both turn a choice on the card into its record
and a redraw; they are not merged (batch 5 of misc-tasks/runs/20261105-bridge-refactor-4a385b86/survey/plan.md),
and where they differ is the owner's to decide: what a workspace may be, whether the current option chosen again
redraws the card, what turning 敏感词脱敏 on does to a model that cannot redact, and the budget another model starts
from (DifferencesTest). Each row is one click on a fresh record: the fields it changed in the saved record (None
when nothing was saved) and whether the card is redrawn."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support import make_context

from covey.app.harness_switch import HarnessSwitch
from covey.app.model_picker import LaunchModelPicker
from covey.cards import switch_form
from covey.cards.launch_config import (BUDGET_ACTION, HARNESS_ACTION, MANAGE_OPTION, MODE_ACTION, MODEL_ACTION,
                                                PAGE_ACTION, PROVIDER_ACTION, REDACTION_ACTION, WORK_MODE_ACTION,
                                                WORKSPACE_ACTION)
from covey.core.state import read_json, write_json
from covey.core.topics import topic_file

CHAT, TOKEN, TOPIC = "oc_chat", "ab" * 12, "t" * 16
OPUS = {"harness": "claude", "label": "Opus", "levels": ["low", "medium", "high"], "work_modes": ["focus", "adaptive"],
        "redaction": True}
SONNET = {"harness": "claude", "label": "Sonnet", "levels": ["high", "xhigh", "max"]}  # Its own default: 推荐.
GATEWAY = {"harness": "claude", "label": "Opus 网关", "levels": ["low", "high"], "provider": "gateway", "redaction": True}
GPT = {"harness": "codex", "label": "GPT", "levels": ["low", "medium", "high", "xhigh"]}
MODELS = {"claude:opus": OPUS, "claude:sonnet": SONNET, "claude:gateway": GATEWAY, "codex:gpt": GPT}
PENDING = {"config_pending": True, "config_revision": 5}  # Every redraw: the next revision, pending until drawn.


def changes(before, after):
    return {key: after.get(key) for key in before.keys() | after.keys() if before.get(key) != after.get(key)}


class ReducerCase(unittest.TestCase):
    folder = ""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ctx = make_context(Path(tmp.name))
        self.state = self.ctx.state_dir
        self.topics = SimpleNamespace(runtime=SimpleNamespace(card_queues=[], access=SimpleNamespace(allows=lambda *_: True)))
        self.redraws = []

    def submitted(self, busy, handler, *args):
        self.redraws.append(handler.__name__)
        return True

    def click(self, controller, record, kind, option, *, page=False):
        path = self.state / self.folder / (TOKEN + ".json")
        write_json(path, record)
        before = read_json(path)
        self.redraws.clear()
        controller.dispatch(self.ctx, {"chat_id": CHAT, "message_id": "om_card",
                                       "action_value": {"kind": kind, "token": TOKEN, **({"page": option} if page else {})},
                                       **({} if page else {"option": option})})
        after = read_json(path)
        return (changes(before, after) or None), bool(self.redraws)

    def check(self, controller, rows, record):
        for name, overrides, kind, option, expected, redrawn in rows:
            with self.subTest(name):
                got = self.click(controller, record(**overrides), kind, option, page=kind in (PAGE_ACTION, switch_form.PAGE))
                self.assertEqual(got, (expected, redrawn))


def launch_record(**overrides):
    return {"schema_version": 3, "status": "ready", "card_id": "card", "message_id": "om_card", "chat_id": CHAT,
            "topic_id": TOPIC, "harness_id": "claude", "binding_revision": 1, "compact": True, "page": "",
            "config_revision": 4, "selected_model": "claude:opus", "selected_effort": "high",
            "form_defaults": {"workspace": "repo", "run_mode": "direct"}, "models": dict(MODELS), **overrides}


def switch_record(**overrides):
    return {"status": "ready", "chat_id": CHAT, "message_id": "om_card", "topic_id": TOPIC, "config_revision": 4,
            "page": "", "notice": "", "selected_model": "claude:opus", "selected_effort": "high",
            "selected_redaction": "off", "selected_workspace": "repo", "selected_run_mode": "direct",
            "workspaces": ["repo", "other"], "current": {"key": "claude:opus", "budget": "high"},
            "usual_efforts": {"codex:gpt": "low"}, "models": dict(MODELS), **overrides}


LAUNCH_ROWS = [
    # name, record fields, kind, option, changed fields, redrawn
    ("another workspace", {}, WORKSPACE_ACTION, "other",
     {"form_defaults": {"workspace": "other", "run_mode": "direct"}, **PENDING}, True),
    ("a workspace no list names", {}, WORKSPACE_ACTION, "unlisted",
     {"form_defaults": {"workspace": "unlisted", "run_mode": "direct"}, **PENDING}, True),
    ("the current workspace", {}, WORKSPACE_ACTION, "repo", None, False),
    ("the current workspace on its page", {"page": "workspace"}, WORKSPACE_ACTION, "repo", {"page": "", **PENDING}, True),
    ("the workspace manager's entry", {}, WORKSPACE_ACTION, MANAGE_OPTION, None, False),
    ("a workspace over 200 characters", {}, WORKSPACE_ACTION, "x" * 201, None, False),
    ("a workspace on a card without the settings list", {"compact": False}, WORKSPACE_ACTION, "other", None, False),
    ("the other run mode", {}, MODE_ACTION, "worktree",
     {"form_defaults": {"workspace": "repo", "run_mode": "worktree"}, **PENDING}, True),
    ("an unknown run mode", {}, MODE_ACTION, "elsewhere", None, False),
    ("a work mode", {}, WORK_MODE_ACTION, "focus", {"selected_work_mode": "focus", **PENDING}, True),
    ("a work mode the model lacks", {}, WORK_MODE_ACTION, "turbo", None, False),
    ("脱敏 on, the model redacts", {}, REDACTION_ACTION, "on", {"selected_redaction": "on", **PENDING}, True),
    ("脱敏 on, the model cannot redact", {"selected_model": "claude:sonnet"}, REDACTION_ACTION, "on", None, False),
    ("another budget", {}, BUDGET_ACTION, "4:low", {"selected_effort": "low", **PENDING}, True),
    ("the current budget", {}, BUDGET_ACTION, "4:high", None, False),
    ("a budget of an older revision", {}, BUDGET_ACTION, "3:low", None, False),
    ("a budget the model lacks", {}, BUDGET_ACTION, "4:ultra", None, False),
    ("a row's page", {}, PAGE_ACTION, "workspace", {"page": "workspace", **PENDING}, True),
    ("the open row's page", {"page": "workspace"}, PAGE_ACTION, "workspace", None, False),
    ("an unknown page", {}, PAGE_ACTION, "nowhere", None, False),
    ("a page on a card without the settings list", {"compact": False}, PAGE_ACTION, "workspace", None, False),
    # A model choice: from the catalogue (here the record's own models), its budget from scratch.
    ("another model", {}, MODEL_ACTION, "claude:sonnet",
     {"selected_model": "claude:sonnet", "selected_effort": "", **PENDING}, True),
    ("the current model", {}, MODEL_ACTION, "claude:opus", None, False),
    ("the current model on its page", {"page": "model"}, MODEL_ACTION, "claude:opus", {"page": "", **PENDING}, True),
    ("a model no longer offered", {}, MODEL_ACTION, "claude:gone", None, False),
    ("another Harness", {}, HARNESS_ACTION, "codex",
     {"selected_model": "codex:gpt", "selected_effort": "", **PENDING}, True),
    ("the current Harness", {}, HARNESS_ACTION, "claude", None, False),
    ("another provider", {}, PROVIDER_ACTION, "gateway",
     {"selected_model": "claude:gateway", "selected_effort": "", **PENDING}, True),
]

SWITCH_ROWS = [
    ("another workspace", {}, switch_form.WORKSPACE, "other", {"selected_workspace": "other", **PENDING}, True),
    ("a workspace no list names", {}, switch_form.WORKSPACE, "unlisted", None, False),
    ("the current workspace", {}, switch_form.WORKSPACE, "repo", PENDING, True),
    ("the other run mode", {}, switch_form.RUN_MODE, "worktree", {"selected_run_mode": "worktree", **PENDING}, True),
    ("an unknown run mode", {}, switch_form.RUN_MODE, "elsewhere", None, False),
    ("a work mode", {}, switch_form.WORK_MODE, "focus", {"selected_work_mode": "focus", **PENDING}, True),
    ("a work mode the model lacks", {}, switch_form.WORK_MODE, "turbo", None, False),
    ("another budget", {}, switch_form.BUDGET, "4:low", {"selected_effort": "low", **PENDING}, True),
    ("the current budget", {}, switch_form.BUDGET, "4:high", PENDING, True),
    ("a budget of an older revision", {}, switch_form.BUDGET, "3:low", None, False),
    ("a budget the model lacks", {}, switch_form.BUDGET, "4:ultra", None, False),
    ("a budget refused on its page", {"page": "effort"}, switch_form.BUDGET, "3:low", {"page": "", **PENDING}, True),
    ("脱敏 on, the model redacts", {}, switch_form.REDACTION, "on", {"selected_redaction": "on", **PENDING}, True),
    ("脱敏 on, the model cannot redact", {"selected_model": "claude:sonnet", "selected_effort": "low"},
     switch_form.REDACTION, "on",
     {"selected_redaction": "on", "selected_model": "claude:opus", "selected_effort": "high", **PENDING}, True),
    ("脱敏 off once it turned off by itself", {"redaction_auto_off": True, "selected_redaction": "on"},
     switch_form.REDACTION, "off", {"selected_redaction": "off", "redaction_auto_off": None, **PENDING}, True),
    ("the current 脱敏", {}, switch_form.REDACTION, "off", None, False),
    ("another model", {}, switch_form.MODEL, "claude:sonnet",
     {"selected_model": "claude:sonnet", "selected_effort": "top_minus_one", **PENDING}, True),
    ("a model the chat usually gives a budget", {}, switch_form.MODEL, "codex:gpt",
     {"selected_model": "codex:gpt", "selected_effort": "low", **PENDING}, True),
    ("the current model", {}, switch_form.MODEL, "claude:opus", None, False),
    ("the current model on its page", {"page": "model"}, switch_form.MODEL, "claude:opus", {"page": "", **PENDING}, True),
    ("a model that cannot redact while 脱敏 is on", {"selected_redaction": "on"}, switch_form.MODEL, "claude:sonnet",
     {"selected_model": "claude:sonnet", "selected_effort": "top_minus_one", "selected_redaction": "off",
      "redaction_auto_off": True, **PENDING}, True),
    ("back to the topic's model after 脱敏 turned off by itself",
     {"selected_model": "claude:sonnet", "selected_effort": "low", "redaction_auto_off": True}, switch_form.MODEL,
     "claude:opus", {"selected_model": "claude:opus", "selected_effort": "high", "selected_redaction": "on",
                     "redaction_auto_off": None, **PENDING}, True),
    ("another Harness", {}, switch_form.HARNESS, "codex",
     {"selected_model": "codex:gpt", "selected_effort": "low", **PENDING}, True),
    ("the current Harness", {}, switch_form.HARNESS, "claude", None, False),
    ("another provider", {}, switch_form.PROVIDER, "gateway",
     {"selected_model": "claude:gateway", "selected_effort": "", **PENDING}, True),  # No default of its own.
    ("a row's page", {}, switch_form.PAGE, "model", {"page": "model", **PENDING}, True),
    ("the open row's page", {"page": "model"}, switch_form.PAGE, "model", None, False),
    ("an unknown page", {}, switch_form.PAGE, "nowhere", None, False),
]


class LaunchCardReducerTest(ReducerCase):
    folder = "launch-forms"

    def setUp(self):
        super().setUp()
        write_json(topic_file(self.state, TOPIC), {"chat_id": CHAT, "harness_id": "claude", "binding_revision": 1})
        self.picker = LaunchModelPicker(self.topics)
        self.picker.submit = self.submitted
        live = mock.patch("covey.app.model_picker.available_models", return_value=dict(MODELS))
        live.start()
        self.addCleanup(live.stop)

    def test_each_choice(self):
        self.check(self.picker, LAUNCH_ROWS, launch_record)


class SwitchCardReducerTest(ReducerCase):
    folder = "switch-forms"

    def setUp(self):
        super().setUp()
        self.switch = HarnessSwitch(self.topics)
        self.switch.submit = self.submitted

    def test_each_choice(self):
        self.check(self.switch, SWITCH_ROWS, switch_record)


class DifferencesTest(ReducerCase):
    """The same choice on each card, where the two reducers part."""

    def setUp(self):
        super().setUp()
        write_json(topic_file(self.state, TOPIC), {"chat_id": CHAT, "harness_id": "claude", "binding_revision": 1})
        self.picker, self.switch = LaunchModelPicker(self.topics), HarnessSwitch(self.topics)
        self.picker.submit = self.switch.submit = self.submitted
        live = mock.patch("covey.app.model_picker.available_models", return_value=dict(MODELS))
        live.start()
        self.addCleanup(live.stop)

    def both(self, launch_kind, switch_kind, option, launch=None, switch=None):
        self.folder = "launch-forms"
        first = self.click(self.picker, launch_record(**(launch or {})), launch_kind, option)
        self.folder = "switch-forms"
        return first, self.click(self.switch, switch_record(**(switch or {})), switch_kind, option)

    def test_a_workspace_the_switch_card_does_not_list_is_recorded_on_the_launch_card_only(self):
        launch, switch = self.both(WORKSPACE_ACTION, switch_form.WORKSPACE, "unlisted")
        self.assertTrue(launch[1])
        self.assertEqual(switch, (None, False))

    def test_the_current_option_again_redraws_the_switch_card_only(self):
        for launch_kind, switch_kind, option in ((WORKSPACE_ACTION, switch_form.WORKSPACE, "repo"),
                                                 (BUDGET_ACTION, switch_form.BUDGET, "4:high")):
            with self.subTest(option):
                self.assertEqual(self.both(launch_kind, switch_kind, option), ((None, False), (PENDING, True)))

    def test_turning_redaction_on_moves_the_switch_card_to_a_model_that_redacts(self):
        sonnet = {"selected_model": "claude:sonnet", "selected_effort": "low"}
        launch, switch = self.both(REDACTION_ACTION, switch_form.REDACTION, "on", sonnet, sonnet)
        self.assertEqual(launch, (None, False))
        self.assertEqual(switch[0]["selected_model"], "claude:opus")

    def test_another_model_starts_from_no_budget_on_the_launch_card_and_from_its_usual_one_on_the_switch_card(self):
        launch, switch = self.both(MODEL_ACTION, switch_form.MODEL, "codex:gpt")
        self.assertEqual((launch[0]["selected_effort"], switch[0]["selected_effort"]), ("", "low"))


if __name__ == "__main__":
    unittest.main()
