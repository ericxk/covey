"""The welcome card (app/welcome.py, cards/welcome.py, core/welcome.py): once per new deployment and never for one in use
before; sent to the owner's open_id or his private chat, a refused send tried again and one that may have landed
never; and its examples, each clicked once, posted to the chat and taken as the owner's own message through the real
Bridge (the butler's turn, or a new topic waiting on its launch card)."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support.butler import switch_on
from support.world import UnifiedCase
from covey.app import welcome as feature
from covey.app.card_actions import ACTIONS, BOT_ONLY
from covey.app.unified import UnifiedBridgeApp
from covey.cards import welcome as cards
from covey.core import butler, welcome
from covey.core.jobs import job_path, read_job_record, upsert_job_record
from covey.core.state import write_json
from covey.core.text import literal
from covey.core.topics import message_topic
from covey.feishu.message_request import configure_top_level
from covey.feishu.transport import Failure, set_failure

CHAT = "oc_dm"
REFUSED = Failure("api_rejected", code="230013", detail="Bot has NO availability to this user.")
TIMEOUT = Failure("network_timeout", True, True)


def texts(card) -> str:
    """Every markdown and button text of a card, one string."""
    found = []

    def walk(node):
        if isinstance(node, dict):
            if isinstance(node.get("content"), str):
                found.append(node["content"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    walk(card)
    return "\n".join(found)


def buttons(card) -> list:
    """(label, value, disabled) of each button of a card, in order."""
    found = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("tag") == "button":
                found.append((node["text"]["content"], node["behaviors"][0]["value"], bool(node.get("disabled"))))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    walk(card)
    return found


VIEW = {"host": "devbox", "harnesses": ["Codex", "Claude Code"], "down": [], "workspace": "work",
        "workdir": "/absolute/work", "butler": "", "examples": list(feature.EXAMPLES)}


class RecordTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name) / "state"
        self.state.mkdir()

    def test_a_new_deployment_is_pending_and_one_used_before_is_skipped(self):
        (self.state / "topics" / "aliases").mkdir(parents=True)  # Aliases alone are no topic.
        write_json(self.state / "main-thread.json", {"harness": "codex"})  # The settings: no history.
        self.assertEqual(welcome.settle(self.state), welcome.PENDING)
        for used in ("job", "topic", "chat"):
            with self.subTest(used=used), tempfile.TemporaryDirectory() as other:
                state = Path(other)
                if used == "job":
                    upsert_job_record(state, "om_1-1", status="done", chat_id=CHAT)
                elif used == "topic":
                    (state / "topics" / "0123456789abcdef").mkdir(parents=True)
                else:
                    write_json(state / "owner-chat.json", {"owner": "owner", "chat_id": CHAT})
                self.assertEqual(welcome.settle(state), welcome.SKIPPED)
                self.assertEqual(welcome.read(state)["why"], "history")

    def test_the_record_decides_once(self):
        self.assertEqual(welcome.settle(self.state), welcome.PENDING)
        upsert_job_record(self.state, "om_1-1", status="done", chat_id=CHAT)  # The owner's first message.
        self.assertEqual(welcome.settle(self.state), welcome.PENDING)  # A restart before the card went: still due.
        welcome.update(self.state, lambda record: {**record, "state": welcome.SENT})
        self.assertEqual(welcome.settle(self.state), welcome.SENT)
        self.assertEqual(welcome.settle(self.state), welcome.SENT)  # Restarts and reinstalls never send it again.

    def test_a_send_going_out_when_the_bridge_stopped_is_uncertain(self):
        write_json(welcome.path(self.state), {"state": welcome.SENDING, "card_id": "c1"})
        self.assertEqual(welcome.settle(self.state), welcome.UNCERTAIN)
        self.assertEqual(welcome.read(self.state)["card_id"], "c1")

    def test_first_answer_follows_the_owners_private_chat(self):
        self.assertEqual(welcome.first_answer(self.state), welcome.NONE)
        write_json(self.state / "owner-chat.json", {"owner": "owner", "chat_id": CHAT})
        (self.state / "jobs").mkdir()
        self.assertEqual(welcome.first_answer(self.state), welcome.NONE)
        upsert_job_record(self.state, "om_g-1", job_id=1, status="done", delivery_ok=True, chat_id="oc_group")
        self.assertEqual(welcome.first_answer(self.state), welcome.NONE)  # Another chat's answer is not his.
        # Each a newer task of the chat: its latest says how far it got.
        for job_id, status, stage in ((2, "waiting_workspace", welcome.LAUNCH), (3, "queued", welcome.RECEIVED),
                                      (4, "running", welcome.RECEIVED), (5, "failed", welcome.STOPPED),
                                      (6, "done", welcome.STOPPED)):  # Done, its reply not delivered.
            with self.subTest(status=status):
                upsert_job_record(self.state, f"om_{job_id}-{job_id}", job_id=job_id, status=status, chat_id=CHAT)
                self.assertEqual(welcome.first_answer(self.state), stage)
        upsert_job_record(self.state, "om_7-7", job_id=7, status="done", delivery_ok=True, chat_id=CHAT)
        upsert_job_record(self.state, "om_8-8", job_id=8, status="failed", chat_id=CHAT)
        self.assertEqual(welcome.first_answer(self.state), welcome.ANSWERED)  # Answered once is enough.


class WorkspaceGuideTest(unittest.TestCase):
    """D1 (T647): ./install's new default workspace gets the guide while empty (core/welcome.py seed_workspace), and it
    counts as holding no project while that is all it holds (bare_workspace)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name) / "workspace"
        self.folder.mkdir()

    def test_an_empty_workspace_is_seeded_as_a_home_once_and_stays_bare(self):
        from covey.core import home
        self.assertTrue(welcome.bare_workspace(self.folder))
        self.assertTrue(welcome.seed_workspace(self.folder))
        self.assertTrue(home.seeded(self.folder))
        self.assertTrue({"AGENTS.md", "CLAUDE.md", ".ignore"} <= {p.name for p in self.folder.iterdir()})
        self.assertTrue(welcome.bare_workspace(self.folder))
        (self.folder / "main.py").write_text("print(1)\n")
        self.assertFalse(welcome.bare_workspace(self.folder))
        self.assertFalse(welcome.seed_workspace(self.folder / "missing"))

    def test_the_guide_an_older_install_wrote_still_counts_as_bare(self):
        (self.folder / welcome.GUIDE).write_text(welcome.GUIDE_TEXT)
        self.assertTrue(welcome.bare_workspace(self.folder))
        self.assertFalse(welcome.seed_workspace(self.folder))  # Not empty: taken over, nothing written.

    def test_anything_else_in_it_is_a_project(self):
        (self.folder / welcome.GUIDE).write_text("# my project\n")  # A README of the owner's own.
        self.assertFalse(welcome.bare_workspace(self.folder))
        self.assertFalse(welcome.seed_workspace(self.folder))
        self.assertEqual((self.folder / welcome.GUIDE).read_text(), "# my project\n")
        (self.folder / welcome.GUIDE).write_text(welcome.GUIDE_TEXT)
        (self.folder / "main.py").write_text("print(1)\n")
        self.assertFalse(welcome.bare_workspace(self.folder))
        self.assertFalse(welcome.bare_workspace(self.folder / "missing"))


class CardTest(unittest.TestCase):
    def test_with_the_butler_it_says_how_a_message_is_answered_and_offers_three_examples(self):
        card = cards.welcome_card({**VIEW, "down": [["Antigravity", "没登录"]]})
        words = texts(card)
        for part in ("欢迎使用 Covey", "装在 devbox", "有事直接在这里说", "管家先接住每句话", "出现表情，就是收到了",
                     "Harness：Codex、Claude Code", "现在用不了：Antigravity（没登录）", "主目录：/absolute/work"):
            self.assertIn(part, words)
        self.assertNotIn("启动卡", words)
        self.assertNotIn("默认工作区：", words)  # The machine's line names the home, not a workspace.
        self.assertEqual(buttons(card), [(cards.TRY, {"kind": cards.KIND, "example": n}, False) for n in range(3)])
        self.assertEqual(card["config"]["summary"]["content"], "欢迎使用 Covey · 有事直接在这里说")

    def test_without_the_butler_it_says_why_how_to_get_it_and_that_a_launch_card_comes(self):
        for why in (cards.NO_CLAUDE, cards.OFF, cards.DOWN):
            with self.subTest(why=why):
                words = texts(cards.welcome_card({**VIEW, "butler": why}))
                reason, how = cards.BUTLER_WORDS[why]
                for part in ("每条消息开一个话题", "「开始任务」", "要管家", literal(reason), literal(how)):
                    self.assertIn(part, words)
                self.assertNotIn("表情", words)
        self.assertIn("装好并登录 Claude Code", texts(cards.welcome_card({**VIEW, "butler": cards.NO_CLAUDE})))

    def test_a_home_with_no_project_yet_says_how_to_add_one(self):
        words = texts(cards.welcome_card({**VIEW, "bare_workspace": True}))
        self.assertIn(cards.BARE_WORKSPACE, words)
        self.assertLess(words.index("/absolute/work"), words.index(cards.BARE_WORKSPACE))
        self.assertIn("「仓库」", cards.BARE_WORKSPACE)
        self.assertNotIn("工作区", cards.BARE_WORKSPACE)
        self.assertNotIn(cards.BARE_WORKSPACE, texts(cards.welcome_card(VIEW)))

    def test_an_example_tried_is_off_and_one_that_failed_is_offered_again(self):
        tried = {"0": {"state": cards.SENT}, "1": {"state": cards.FAILED}, "2": {"state": cards.UNCERTAIN}}
        card = cards.welcome_card({**VIEW, "tried": tried, "notice": "「这个工作区里有什么」没发出去，再点一次。"})
        self.assertEqual([(label, disabled) for label, _, disabled in buttons(card)],
                         [(cards.TRIED_LABEL, True), (cards.TRY, False), (cards.TRIED_LABEL, True)])
        self.assertIn("没发出去，再点一次", texts(card))

    def test_the_kind_starts_a_task_and_is_the_bridges_own(self):
        self.assertIn(cards.KIND, BOT_ONLY)
        self.assertEqual(ACTIONS[cards.KIND].handler, "")
        self.assertIn("welcome", UnifiedBridgeApp.PANELS)
        self.assertIn("welcome", UnifiedBridgeApp.PARTS)


class WelcomeCase(UnifiedCase):
    """The real Bridge, a new deployment's state directory, the card's Feishu calls recorded."""

    def setUp(self):
        super().setUp()
        self.addCleanup(set_failure, None)
        messenger = self.app.messenger
        messenger.create_card = mock.Mock(return_value={"code": 0, "data": {"card_id": "card_w"}})
        messenger.send_card = mock.Mock(return_value=("om_welcome", False))
        messenger.send_card_to_user = mock.Mock(return_value=("om_welcome", CHAT))
        messenger.send_text = mock.Mock(side_effect=lambda profile, chat, text, key: f"om_ex{len(messenger.send_text.mock_calls)}")
        messenger.put_card = mock.Mock(return_value={"code": 0})
        self.welcome = self.app.welcome
        self.state = self.ctx.state_dir

    def record(self):
        return welcome.read(self.state)

    def sent(self):
        """A new deployment's card, sent to the owner's open_id."""
        self.welcome.settle(self.ctx)
        self.assertEqual(self.welcome.send(self.ctx, ""), welcome.SENT)

    def click(self, example, event_id="ev1", message="om_welcome", operator="owner"):
        self.app.handle_card_action_event({"event_id": event_id, "operator_id": operator, "chat_id": CHAT,
                                           "message_id": message, "action_value": {"kind": cards.KIND, "example": example}},
                                          self.ctx)
        return [line.split("outcome=")[1].split()[0] for line in self.logs if line.startswith("card action ")][-1]

    def dm(self, mid, content="任务"):
        with mock.patch("covey.app.app.enrich_incoming_event") as enrich:
            event = {"chat_id": CHAT, "chat_type": "p2p", "sender_type": "user", "sender_id": "owner",
                     "message_id": mid, "content": content}
            enrich.return_value = SimpleNamespace(text=content, display_text=content, event=event)
            self.app.handle_event(event, self.ctx)


class SendTest(WelcomeCase):
    def test_a_new_deployment_gets_it_once_at_the_owners_open_id_and_the_chat_is_saved(self):
        self.welcome.settle(self.ctx)
        self.assertEqual([name for name, _ in self.welcome.threads()], ["welcome"])
        self.assertEqual(self.welcome.send(self.ctx, ""), welcome.SENT)
        self.app.messenger.send_card_to_user.assert_called_once()
        profile, owner, card_id, key = self.app.messenger.send_card_to_user.call_args.args
        self.assertEqual((owner, card_id), ("owner", "card_w"))
        self.assertLessEqual(len(key), 50)
        self.app.messenger.send_card.assert_not_called()
        record = self.record()
        self.assertEqual((record["state"], record["message_id"], record["chat_id"], record["sequence"]),
                         (welcome.SENT, "om_welcome", CHAT, 0))
        self.assertEqual(self.app.runtime.access.owner_chat(), CHAT)  # Menus, digests and the rest find it now.
        self.assertEqual(record["view"]["examples"], list(feature.TOPIC_EXAMPLES))
        self.assertIn("Claude Code", record["view"]["harnesses"])
        self.assertEqual(record["view"]["workspace"], "project")
        self.assertEqual(self.welcome.threads(), ())  # Sent: no thread at the next start.
        self.welcome.settle(self.ctx)
        self.assertEqual(self.record()["state"], welcome.SENT)

    def test_the_view_says_while_the_default_workspace_holds_no_project(self):
        workdir = Path(self.welcome.view(self.ctx)["workdir"])
        for item in list(workdir.iterdir()):
            if item.is_file():
                item.unlink()
        if any(workdir.iterdir()):
            self.skipTest("the test world's workspace holds directories")
        self.assertTrue(self.welcome.view(self.ctx).get("bare_workspace"))
        welcome.seed_workspace(workdir)
        self.assertTrue(self.welcome.view(self.ctx).get("bare_workspace"))
        (workdir / "app.py").write_text("print(1)\n")
        self.assertNotIn("bare_workspace", self.welcome.view(self.ctx))

    def test_once_the_owner_wrote_it_goes_to_his_private_chat(self):
        self.welcome.settle(self.ctx)
        self.dm("om_first")  # His first message names the chat (app/access.py).
        self.assertEqual(self.welcome.send(self.ctx, self.app.runtime.access.owner_chat()), welcome.SENT)
        self.assertEqual(self.app.messenger.send_card.call_args.args[1:3], (CHAT, "card_w"))
        self.app.messenger.send_card_to_user.assert_not_called()

    def test_a_refused_send_is_tried_again_and_one_that_may_have_landed_never(self):
        self.welcome.settle(self.ctx)

        def refused(*args):
            set_failure(REFUSED)
            return "", ""
        self.app.messenger.send_card_to_user.side_effect = refused
        self.assertEqual(self.welcome.send(self.ctx, ""), welcome.PENDING)
        self.assertEqual((self.record()["state"], self.record()["card_id"], self.record()["tries"]), (welcome.PENDING, "", 1))
        self.assertTrue(any("welcome card pending via=user try=1 category=api_rejected code=230013" in line for line in self.logs))

        def timed_out(*args, **kwargs):
            set_failure(TIMEOUT)
            return "", False
        self.app.messenger.send_card.side_effect = timed_out
        self.assertEqual(self.welcome.send(self.ctx, CHAT), welcome.UNCERTAIN)
        keys = {self.app.messenger.send_card_to_user.call_args.args[3], self.app.messenger.send_card.call_args.args[3]}
        self.assertEqual(len(keys), 2)  # Another try, another key: the refused one did not land.
        self.welcome.settle(self.ctx)
        self.assertEqual(self.record()["state"], welcome.UNCERTAIN)
        self.assertEqual(self.welcome.threads(), ())
        self.assertEqual(self.welcome.send(self.ctx, CHAT), welcome.UNCERTAIN)  # Never sent again (rule 1).
        self.assertEqual(self.app.messenger.send_card.call_count, 1)

    def test_a_deployment_in_use_before_gets_nothing(self):
        self.dm("om_old")  # Before this release: a task and the private chat.
        self.welcome.settle(self.ctx)
        self.assertEqual(self.record()["state"], welcome.SKIPPED)
        self.assertEqual(self.welcome.threads(), ())
        self.app.messenger.create_card.assert_not_called()

    def test_off_writes_and_sends_nothing(self):
        with mock.patch.dict(os.environ, {feature.SWITCH: "0"}):
            self.welcome.settle(self.ctx)
            self.assertFalse(welcome.path(self.state).exists())
            self.assertEqual(self.welcome.threads(), ())
        self.welcome.settle(self.ctx)  # Turned on later: decided then, by what the state directory holds.
        self.assertEqual(self.record()["state"], welcome.PENDING)

    def test_the_thread_tries_the_open_id_then_the_chat_his_first_message_names(self):
        self.welcome.settle(self.ctx)
        waits = []

        def refused(*args):
            set_failure(REFUSED)
            return "", ""
        self.app.messenger.send_card_to_user.side_effect = refused

        def wait(seconds=None):
            waits.append(seconds)
            if len(waits) == 3:
                self.dm("om_first")  # The owner writes while it polls.
            return seconds is None or len(waits) > 20
        self.addCleanup(setattr, self.app.runtime, "shutdown", self.app.runtime.shutdown)
        self.app.runtime.shutdown = SimpleNamespace(wait=wait, is_set=lambda: False)
        self.welcome.run(self.ctx)
        self.assertEqual(waits[0], feature.FIRST)
        self.assertEqual(self.app.messenger.send_card_to_user.call_count, 1)  # Not again within RETRY.
        self.assertEqual(self.app.messenger.send_card.call_args.args[1], CHAT)  # At once once the chat is known.
        self.assertEqual(self.record()["state"], welcome.SENT)
        self.assertIsNone(waits[-1])  # Parked: supervised would only start it again.


class ClickTest(WelcomeCase):
    def test_a_click_posts_the_example_and_it_opens_a_topic_with_its_launch_card(self):
        with mock.patch.dict(os.environ, {butler.ENV: "0"}):  # No butler: every message opens a topic.
            self.sent()
            self.assertEqual(self.record()["view"]["butler"], cards.OFF)
            self.assertEqual(self.click(1), "welcome_try")
        text = feature.TOPIC_EXAMPLES[1]
        profile, chat, words, key = self.app.messenger.send_text.call_args.args
        self.assertEqual((chat, words), (CHAT, text))
        record = read_job_record(job_path(self.state, "om_ex1"))  # The posted message is the owner's message.
        self.assertEqual((record["status"], record["user_text"], record["sender_id"]), ("waiting_workspace", text, "owner"))
        topic = message_topic(self.state, CHAT, "om_ex1")
        self.assertTrue(topic)
        launch = next(app for app in self.app.apps.values() if app.submission.selection_card.called)
        self.assertEqual(launch.submission.selection_card.call_args.args[2], "om_ex1")
        self.assertEqual(self.record()["tried"]["1"]["state"], cards.SENT)
        redrawn = self.app.messenger.put_card.call_args
        self.assertEqual(redrawn.kwargs["sequence"], 1)
        self.assertEqual([(label, disabled) for label, _, disabled in buttons(redrawn.args[2])],
                         [(cards.TRY, False), (cards.TRIED_LABEL, True), (cards.TRY, False)])
        self.assertEqual(self.click(1), "duplicate")  # The same click again: Feishu delivers at least once.
        self.assertEqual(self.click(1, "ev2"), "welcome_again")  # Another click on it: never twice.
        self.assertEqual(self.app.messenger.send_text.call_count, 1)

    def test_with_the_butler_the_example_is_the_butlers_turn_with_reactions_on_it(self):
        switch_on(self)  # The butler (COVEY_BUTLER not 0, core/butler.py enabled).
        self.addCleanup(configure_top_level, None)
        self.sent()
        self.assertEqual(self.record()["view"]["butler"], "")
        self.assertEqual(self.click(0), "welcome_try")
        jobs = self.app.runtime.jobs.topic_jobs(butler.butler_topic_id(CHAT), CHAT)
        self.assertEqual([(job.message_id, job.user_text) for job in jobs], [("om_ex1", feature.EXAMPLES[0])])
        self.assertEqual(self.app.messenger.react.call_args.args[1:], ("om_ex1", "OnIt"))  # The reaction the card speaks of.
        self.app.messenger.send_message.assert_not_called()  # Nothing else said: the butler's turn answers.

    def test_the_examples_take_him_round_with_the_butler_and_without_it(self):
        words = feature.EXAMPLES, feature.TOPIC_EXAMPLES
        self.assertTrue(all(len(each) == 3 for each in words))
        # With the butler the first two say "open a topic" (it answers a minute's work in place); the third is the 看板.
        self.assertTrue(all(text.startswith(feature.OPEN) for text in feature.EXAMPLES[:2]))
        self.assertIn("看板", feature.EXAMPLES[2])
        # Without it every message is a topic: no such words, the third an ordinary question.
        self.assertFalse(any(feature.OPEN in text for text in feature.TOPIC_EXAMPLES))
        self.assertEqual(feature.TOPIC_EXAMPLES[:2], tuple(text[len(feature.OPEN):] for text in feature.EXAMPLES[:2]))
        self.assertIn("问我", feature.TOPIC_EXAMPLES[1])  # The choice that comes back as a question card.

    def test_the_card_is_sent_with_the_examples_of_its_mode(self):
        with mock.patch.dict(os.environ, {butler.ENV: "0"}):
            self.sent()
            self.assertEqual(self.record()["view"]["examples"], list(feature.TOPIC_EXAMPLES))

    def test_with_the_butler_the_card_is_sent_with_the_butlers_examples(self):
        switch_on(self)
        self.addCleanup(configure_top_level, None)
        self.sent()
        self.assertEqual((self.record()["view"]["butler"], self.record()["view"]["examples"]), ("", list(feature.EXAMPLES)))
        self.assertTrue(self.record()["view"]["examples"][0].startswith(feature.OPEN))

    def test_only_the_owner_and_only_on_the_card_sent(self):
        self.sent()
        self.assertEqual(self.click(0, operator="ou_someone"), "not_owner")
        self.assertEqual(self.welcome.claim(self.ctx, {"event_id": "ev9", "message_id": "om_welcome", "chat_id": CHAT},
                                            {"kind": cards.KIND, "example": 0}, False), "owner_only")
        self.assertEqual(self.click(0, "ev2", message="om_other"), "welcome_stale")
        self.assertEqual(self.click(7, "ev3"), "welcome_stale")
        self.assertIsNone(self.welcome.claim(self.ctx, {}, {"kind": "other"}, True))
        self.app.messenger.send_text.assert_not_called()

    def test_an_example_that_did_not_go_out_may_be_clicked_again_one_that_may_have_never(self):
        self.sent()

        def refused(*args):
            set_failure(REFUSED)
            return ""
        self.app.messenger.send_text.side_effect = refused
        self.assertEqual(self.click(2), "welcome_try")
        self.assertEqual(self.record()["tried"]["2"]["state"], cards.FAILED)
        self.assertIn("没发出去，再点一次", self.record()["notice"])
        last = self.app.messenger.put_card.call_args
        self.assertEqual(buttons(last.args[2])[2][0], cards.TRY)  # Drawn again: offered again.

        def timed_out(*args):
            set_failure(TIMEOUT)
            return ""
        self.app.messenger.send_text.side_effect = timed_out
        self.assertEqual(self.click(2, "ev2"), "welcome_try")
        self.assertEqual(self.record()["tried"]["2"]["state"], cards.UNCERTAIN)
        self.assertIn("可能没发出去", self.record()["notice"])
        self.assertEqual(self.click(2, "ev3"), "welcome_again")
        self.assertEqual(self.app.messenger.send_text.call_count, 2)
        keys = [call.args[3] for call in self.app.messenger.send_text.call_args_list]
        self.assertEqual(len(set(keys)), 2)
        self.assertEqual(self.app.runtime.jobs.topic_jobs(butler.butler_topic_id(CHAT), CHAT), [])

    def test_a_click_on_a_card_whose_send_was_unsure_says_it_landed(self):
        self.welcome.settle(self.ctx)

        def timed_out(*args):
            set_failure(TIMEOUT)
            return "", ""
        self.app.messenger.send_card_to_user.side_effect = timed_out
        self.assertEqual(self.welcome.send(self.ctx, ""), welcome.UNCERTAIN)
        self.assertEqual(self.click(0, message="om_landed"), "welcome_try")  # Only that card carries the kind.
        record = self.record()
        self.assertEqual((record["state"], record["message_id"], record["chat_id"]), (welcome.SENT, "om_landed", CHAT))
        self.assertEqual(self.app.messenger.send_text.call_args.args[1:3], (CHAT, feature.TOPIC_EXAMPLES[0]))
        self.assertEqual(self.app.messenger.put_card.call_args.args[1], "card_w")
        self.assertEqual(self.app.runtime.access.owner_chat(), CHAT)  # The example, the owner's message, names it.

    def test_sequences_only_grow(self):
        self.sent()
        self.assertTrue(self.welcome.redraw(self.ctx))
        self.assertTrue(self.welcome.redraw(self.ctx))
        self.assertEqual([call.kwargs["sequence"] for call in self.app.messenger.put_card.call_args_list], [1, 2])
        self.assertEqual(self.record()["sequence"], 2)
        self.assertEqual(json.loads(welcome.path(self.state).read_text())["state"], welcome.SENT)


if __name__ == "__main__":
    unittest.main()
