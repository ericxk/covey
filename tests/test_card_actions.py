"""A card click on the way in (app/app.py handle_card_action_event): a failed context lookup names the action it
tried."""
import unittest
from unittest import mock

from support.world import TopicFixture


class CardActionIngressTest(TopicFixture, unittest.TestCase):
    def test_context_lookup_failure_names_the_attempted_action(self):
        self.app.runtime.owner_open_id = "ou_user"
        self.app.messenger.reply = mock.Mock()
        event = {"chat_id":"oc_chat", "operator_id":"ou_user", "message_id":"unknown",
                 "action_value":{"kind":"bridge_artifact_send"}}
        with mock.patch('covey.app.app.has_topic_message', return_value=False), mock.patch('covey.feishu.message_read.fetch_message_detail', side_effect=OSError("network")):
            self.app.handle_card_action_event(event, self.ctx)
        text = self.app.messenger.reply.call_args.args[2]
        self.assertIn("发送产物暂未完成", text)
        self.assertNotIn("提交回答", text)
