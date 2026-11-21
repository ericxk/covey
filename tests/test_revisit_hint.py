"""An ordinary topic's answer that puts something off with nothing to bring it back (app/revisit_hint.py): one grey line, and
with 到点回来看 on a button that sets the topic's revisit. Display only; the owner's click is answered at once."""
from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from support.records import write_topic
from test_revisits import RevisitCase
from covey.app import revisit_hint
from covey.app.card_actions import ACTIONS
from covey.cards import answer_card
from covey.core import revisits as records
from covey.core.state import write_json

PUT_OFF = "训练已经拉起来了，稍后我再看结果。"


class HintCase(RevisitCase):
    def unwatched(self, text=PUT_OFF, done=True, checkpoint=None, snapshot=None, topic=None):
        path = ""
        if checkpoint is not None:
            path = self.root / "hint-checkpoint.json"
            write_json(path, checkpoint, ensure_ascii=False)
            path = str(path)
        return revisit_hint.unwatched(self.state, topic_id=topic or self.topic, chat_id="chat", text=text, done=done,
                                      checkpoint_path=path, snapshot=snapshot)

    def click_event(self, operator="owner", topic=None):
        return {"event_id": "ev1", "chat_id": "chat", "operator_id": operator,
                "action_value": json.dumps({"kind": answer_card.REVISIT_HINT_KIND, "topic_id": topic or self.topic, "job_id": 1})}


class UnwatchedTest(HintCase):
    def test_only_a_done_turn_that_puts_off_with_nothing_to_come_back(self):
        self.assertTrue(self.unwatched())
        self.assertTrue(self.unwatched(checkpoint={"phase": "等训练"}))
        self.assertFalse(self.unwatched(text="训练已经跑完，结果在上面。"))
        self.assertFalse(self.unwatched(done=False))
        self.assertFalse(self.unwatched(checkpoint={"revisit": {"minutes": 30, "look": "看训练"}}))

    def test_a_subtask_still_running_or_a_topic_that_may_not_ask_is_left_alone(self):
        live = {"agents": {}, "children": {"a": {"agents": {"a": {"status": "running"}}, "children": {}}}}
        self.assertFalse(self.unwatched(snapshot=live))
        self.assertTrue(self.unwatched(snapshot={"agents": {"a": {"status": "completed"}}, "children": {}}))
        write_topic(self.state, {"proxy": {"contact": "user:x"}}, topic_id="p" * 64)
        self.assertFalse(self.unwatched(topic="p" * 64))

    def test_the_button_is_there_only_with_the_switch_on(self):
        kwargs = dict(topic_id=self.topic, chat_id="chat", job_id=1, text=PUT_OFF, done=True)
        [element] = revisit_hint.elements(self.state, **kwargs)
        text = json.dumps(element, ensure_ascii=False)
        self.assertIn(answer_card.REVISIT_HINT_LINE, text)
        self.assertIn(answer_card.REVISIT_HINT_KIND, text)
        with mock.patch.dict(os.environ, {records.SWITCH: ""}):
            [bare] = revisit_hint.elements(self.state, **kwargs)
        self.assertIn(answer_card.REVISIT_HINT_LINE, json.dumps(bare, ensure_ascii=False))
        self.assertNotIn(answer_card.REVISIT_HINT_KIND, json.dumps(bare, ensure_ascii=False))
        self.assertEqual(revisit_hint.elements(self.state, **{**kwargs, "text": "好了"}), [])


class ClickTest(HintCase):
    def test_the_table_has_it_not_bot_only_and_with_its_handler(self):
        action = ACTIONS[answer_card.REVISIT_HINT_KIND]
        self.assertEqual((action.handler, action.bot_only), ("controls.revisit_hint", False))

    def test_the_owner_sets_the_revisit_once_and_says_so(self):
        app, ctx = self.route("src")
        controls = app.controls
        self.assertEqual(self.record(), {})
        controls.messenger.reply = mock.Mock(return_value="")
        self.assertTrue(controls.revisit_hint(ctx, self.click_event()))
        saved = self.record()
        self.assertEqual((saved["state"], saved["minutes"], saved["round"]), (records.PENDING, 30, 1))
        self.assertEqual(saved["look"], revisit_hint.LOOK)
        self.assertTrue(controls.revisit_hint(ctx, self.click_event()))
        self.assertEqual(self.record()["set_at"], saved["set_at"])  # A second click changes nothing.
        said = [call.args[2] for call in controls.messenger.reply.call_args_list]
        self.assertTrue(any("约 30 分钟后" in text for text in said))
        self.assertTrue(any("已经约好" in text for text in said))

    def test_someone_else_or_another_topic_sets_nothing(self):
        app, ctx = self.route("src")
        controls = app.controls
        with self.assertRaises(ValueError):
            controls.revisit_hint(ctx, self.click_event(operator="ou_other"))
        self.assertTrue(controls.revisit_hint(ctx, self.click_event(topic="other")))
        self.assertEqual(self.record(), {})


if __name__ == "__main__":
    unittest.main()
