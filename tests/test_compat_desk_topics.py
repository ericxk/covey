"""Front-desk topics (前台话题, docs/butler.md 前台话题（已下线）): the topics the butler opened with
main-thread.json's desk_topics on (11-07) are frozen by core/migrate.py desk-retire (2026-11-10). ``old_desk`` makes
one as a release before left it. The migration marks it once; from then on a top-level message quoting its card goes
to the butler, a click on its cards gets one notice and does nothing, and the butler's tools say it is
retired."""
from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest import mock

from test_butler_topics import CHAT, ButlerTopicsCase
from support.butler import switch_on

from covey.app import butler_topics
from covey.app.butler_env import reply
from covey.app.butler import HARNESS, Butler
from covey.app.butler_tools import TOOLS as BUTLER_TOOLS
from covey.core import butler, butler_settings, migrate
from covey.core.jobs import find_job_record, job_key, upsert_job_record
from covey.core.butler_requests import Request
from covey.core.butler import butler_topic_id
from covey.core.state import read_object, write_json
from covey.core.topics import (RETIRED_TEXT, context_for_topic, load_topic, message_topic, read_topic,
                                        remember_topic_message, retired_topic, topic_key, topic_label)
from covey.core.workspaces import topic_context
from covey.feishu.message_request import configure_top_level

OWNER = "om_owner1"  # The owner's top-level message the butler job "main-job" answers.


class DeskCase(ButlerTopicsCase):
    def setUp(self):
        super().setUp()
        switch_on(self)
        previous_top, previous_state = configure_top_level(None), butler.configure_state(None)
        self.addCleanup(configure_top_level, previous_top)
        self.addCleanup(butler.configure_state, previous_state)
        Butler.configure(self.ctx)  # As the Bridge and every task runner do (app/unified.py build_context).
        self.main = butler_topic_id(CHAT)
        remember_topic_message(self.ctx.state_dir, self.main, CHAT, OWNER)  # app/butler.py handle_event.
        upsert_job_record(self.ctx.state_dir, "main-job", job_id=1, message_id=OWNER, chat_id=CHAT,
                          topic_id=self.main, reply_to_message_id=OWNER)

    def opened(self, request_id="open", **args):
        """A topic topic_start opens: a 话题区 (test_topic_area.py)."""
        answer = self.start(request_id, **args)
        self.assertTrue(answer["ok"], answer)
        return answer["data"], self.job(request_id)

    def old_desk(self, request_id="open", **args):
        """A front-desk topic as a release before 2026-11-09 left it, opened on OWNER by the butler job
        "main-job" (desk_topics on): registered, its root "main-<token>" no message, topic.json quoting OWNER, its
        request's record answered, its task card om_card1."""
        fields = dict(title="查日志", prompt="看看昨晚的失败", workspace="project", readonly=True,
                      user_requested=False, why="日志在这个仓库")
        fields.update(args)
        request = Request(request_id, "topic_start", fields, "main-job", 0.0)
        root = "main-" + butler_topics.request_token(request)
        plan = butler_topics.plan_start(self.env, fields, butler_settings.load(self.ctx.state_dir))
        app = self.env.topics.apps[plan["harness"]]
        registry = self.ctx.state_dir / migrate.DESK_REGISTRY
        registry.mkdir(parents=True, exist_ok=True)
        (registry / topic_key(CHAT, root)).touch()  # The registry of the releases of 11-07 to 11-09.
        event = {"chat_id": CHAT, "message_id": root, "root_id": root}
        target = topic_context(app.harness.context, event, app.store.sessions_dir)
        data = read_topic(target)
        data.update(harness_id=plan["harness"], binding_revision=1, quote=OWNER, model=plan["model"],
                    effort=plan["effort"], model_settings_snapshot=True, workspace_name=plan["workspace"],
                    workdir=plan["workdir"], workspace_locked=True, harness_locked=True,
                    origin={"kind": "main", "request": request_id, "job_key": "main-job", "why": fields["why"],
                            "readonly": fields["readonly"], "user_requested": fields["user_requested"],
                            "title": fields["title"], "at": time.time(), "desk": True})
        write_json(target.history_path.parent / "topic.json", data, ensure_ascii=False, indent=2)
        label = topic_label(self.ctx.state_dir, target.topic_id)
        answer = reply(True, f"已开 {label}「{fields['title']}」：直接开工，它的卡在顶层引用主人这条消息",
                       topic_id=target.topic_id, label=label, root_message_id=root, started=True)
        write_json(butler_topics.record_path(self.ctx.state_dir, request),
                   {"request": request_id, "job_key": "main-job", "chat_id": CHAT, "title": fields["title"],
                    "why": fields["why"], "plan": {**plan, "start": True}, "at": time.time(), "sequence": 0,
                    "desk": OWNER, "area_root": "", "area_taken": False, "root_message_id": root,
                    "topic_id": target.topic_id, "answer": answer},
                   ensure_ascii=False)
        remember_topic_message(self.ctx.state_dir, target.topic_id, CHAT, "om_card1")  # Its task card.
        upsert_job_record(self.ctx.state_dir, job_key(root, 1), job_id=1, status="done", chat_id=CHAT,
                          topic_id=target.topic_id, message_id=root, card_message_id="om_card1",
                          reply_to_message_id=OWNER)
        return answer["data"]


    def frozen(self):
        data = self.old_desk()
        migrate.migrate(self.ctx.state_dir)
        return data

    def notices(self):
        """The texts the default app's controls sent as messages of their own (app/task_controls.py retired_action)."""
        return [c.args[2] for c in self.send.call_args_list]

    def setUp_send(self):
        controls = self.app.topics.apps[self.app.topics.default].controls
        self.send = mock.patch.object(controls.messenger, "send_message", return_value=True).start()
        self.addCleanup(mock.patch.stopall)


class MigrationTest(DeskCase):
    def test_it_marks_each_registered_topic_once_and_keeps_every_field(self):
        data = self.old_desk()
        other = self.start("plain")["data"]["topic_id"]  # A 话题区 the registry does not name.
        before = load_topic(self.ctx.state_dir, data["topic_id"])
        self.assertEqual(migrate.pending(self.ctx.state_dir)[migrate.DESK_RETIRE], 1)
        summary = migrate.migrate(self.ctx.state_dir)
        self.assertEqual(summary[migrate.DESK_RETIRE], {"written": 1, "failed": 0})
        after = load_topic(self.ctx.state_dir, data["topic_id"])
        self.assertEqual(after, {**before, "retired": "desk"})  # An older release reads it as it was.
        self.assertEqual(retired_topic(self.ctx.state_dir, data["topic_id"]), "desk")
        self.assertEqual(retired_topic(self.ctx.state_dir, other), "")
        self.assertNotIn("retired", load_topic(self.ctx.state_dir, other))
        # Done: marked in migrations.json, not run again; the step itself writes nothing a second time.
        self.assertNotIn(migrate.DESK_RETIRE, migrate.migrate(self.ctx.state_dir))
        self.assertIn(migrate.DESK_RETIRE, migrate.done(self.ctx.state_dir))
        self.assertEqual(migrate.retire_desk_topics(self.ctx.state_dir), (0, []))
        self.assertNotIn(migrate.DESK_RETIRE, migrate.pending(self.ctx.state_dir))

    def test_a_registry_entry_without_its_topic_is_nothing_to_do(self):
        registry = self.ctx.state_dir / migrate.DESK_REGISTRY
        registry.mkdir(parents=True)
        (registry / ("f" * 64)).touch()
        self.assertEqual(migrate.retire_desk_topics(self.ctx.state_dir), (0, []))
        self.assertEqual(migrate.pending(self.ctx.state_dir)[migrate.DESK_RETIRE], 0)


class FrozenTest(DeskCase):
    def setUp(self):
        super().setUp()
        self.setUp_send()

    def test_a_message_quoting_its_card_goes_to_the_butler(self):
        data = self.frozen()
        event = {"chat_id": CHAT, "chat_type": "p2p", "message_id": "om_follow1", "root_id": OWNER,
                 "parent_id": "om_card1"}
        thread = Butler(self.app.topics)
        with mock.patch.object(self.app.runtime.access, "owner_chat", return_value=CHAT):
            self.assertIs(thread.app_for(self.ctx, event, True), self.app.topics.apps[HARNESS])
        remember_topic_message(self.ctx.state_dir, self.main, CHAT, "om_follow1")  # handle_event, before the context.
        ctx = context_for_topic(self.ctx, event, lambda w: w)
        self.assertEqual(ctx.topic_id, self.main)
        self.assertNotEqual(message_topic(self.ctx.state_dir, CHAT, "om_follow1"), data["topic_id"])

    def test_a_message_whose_reply_chain_starts_at_its_card_runs_in_the_butler(self):
        # Review ra P4 (r4): the chain's root alias is the frozen topic's, and context_for_topic looks at it first.
        data = self.frozen()
        event = {"chat_id": CHAT, "chat_type": "p2p", "sender_id": "owner", "message_id": "om_follow2",
                 "root_id": "om_card1", "parent_id": "om_card1", "content": "接着查"}
        claude = self.app.topics.apps[HARNESS]
        thread = Butler(self.app.topics)
        enriched = SimpleNamespace(text="接着查", display_text="接着查", event=event)
        with mock.patch.object(self.app.runtime.access, "owner_chat", return_value=CHAT), \
                mock.patch.object(self.app.runtime.access, "allows", return_value=True), \
                mock.patch("covey.app.app.enrich_incoming_event", return_value=enriched), \
                mock.patch.object(claude.submission, "enqueue_chat_job") as enqueue:
            self.assertTrue(thread.handle_event(self.ctx, event, True))
            target = enqueue.call_args.args[0]
            self.assertEqual((target.topic_id, target.reply_to_message_id), (self.main, "om_follow2"))
            self.assertEqual(message_topic(self.ctx.state_dir, CHAT, "om_follow2"), self.main)
            frozen = self.ctx.state_dir / "topics" / data["topic_id"] / "history.jsonl"
            self.assertNotIn("接着查", frozen.read_text() if frozen.exists() else "")
            # Claude Code not available: no butler, so the message is routed as any other and only refused.
            with mock.patch.object(claude.harness, "refuse_while_unavailable", side_effect=RuntimeError("off")):
                follow = {**event, "message_id": "om_follow3", "event_id": "ev-follow3"}
                self.assertFalse(thread.handle_event(self.ctx, follow, True))
                self.assertIsNone(self.app.topics.route_or_refuse(self.ctx, follow))
        self.assertEqual(enqueue.call_count, 1)
        self.assertEqual(self.notices(), [RETIRED_TEXT])

    def test_a_message_routed_into_it_gets_the_notice_and_no_job(self):
        data = self.frozen()
        event = {"chat_id": CHAT, "message_id": "om_in1", "parent_id": "om_card1", "event_id": "ev-in1"}
        self.assertIsNone(self.app.topics.route_or_refuse(self.ctx, event))
        self.assertEqual(self.notices(), [RETIRED_TEXT])
        self.assertNotIn("reply_to_message_id", self.send.call_args.kwargs)  # At the top level, quoting nothing.
        self.assertIsNone(find_job_record(self.ctx.state_dir, job_key("om_in1", 2)))
        self.assertIsNone(self.app.topics.target(self.ctx.state_dir, data["topic_id"], CHAT))

    def test_every_button_on_its_cards_only_says_so(self):
        data = self.frozen()
        for n, kind in enumerate(("bridge_job_stop", "bridge_question", "bridge_switch_open", "bridge_fork")):
            with self.subTest(kind=kind):
                event = {"chat_id": CHAT, "message_id": "om_card1", "operator_id": "owner", "event_id": f"ev-{n}",
                         "action_value": {"kind": kind, "topic_id": data["topic_id"], "job_id": 1}}
                with mock.patch.object(self.app.runtime.access, "allows", return_value=True):
                    self.assertEqual(self.app._handle_card_action_event(event, self.ctx), "retired_topic")
        self.assertEqual(self.notices(), [RETIRED_TEXT] * 4)
        self.assertEqual(self.app.messenger.put_card.call_count, 0)

    def test_the_butlers_tools_say_it_is_retired(self):
        data = self.frozen()
        label = topic_label(self.ctx.state_dir, data["topic_id"])
        for tool, args in (("topic_send", {"topic": label, "text": "再跑一次"}),
                           ("topic_fork", {"topic": label, "title": "换个思路", "prompt": "试试"})):
            with self.subTest(tool=tool):
                answer = self.call(tool, **args)
                self.assertFalse(answer["ok"])
                self.assertIn("已下线的前台话题", answer["text"])
        answer = BUTLER_TOOLS["question_answer"](self.env, Request("qa", "question_answer", {"topic": label}, "main-job",
                                                                0.0))
        self.assertFalse(answer["ok"])
        self.assertIn("已下线的前台话题", answer["text"])
        self.assertEqual(self.notices(), [])

    def test_desk_topics_true_in_butler_json_is_read_as_false(self):
        path = self.ctx.state_dir / butler_settings.FILE
        path.write_text(json.dumps({"mode": "agent", "desk_topics": True}))
        self.assertTrue(butler_settings.desk_topics_left(self.ctx.state_dir))
        self.assertNotIn("desk_topics", butler_settings.to_json(butler_settings.load(self.ctx.state_dir)))
        butler_settings.update(self.ctx.state_dir, model="opus")
        self.assertIs(read_object(path)["desk_topics"], True)  # Kept for an older release.


class OwnerMessageTest(DeskCase):
    def test_owner_message_reads_the_butler_job(self):
        from covey.app.relay_targets import butler_job_owner_message
        self.assertEqual(butler_job_owner_message(self.ctx.state_dir, "main-job"), OWNER)
        self.assertEqual(butler_job_owner_message(self.ctx.state_dir, "nothing"), "")
        upsert_job_record(self.ctx.state_dir, job_key("om_9", 9), job_id=9, message_id="om_9", reply_to_message_id="")
        self.assertEqual(butler_job_owner_message(self.ctx.state_dir, job_key("om_9", 9)), "om_9")
