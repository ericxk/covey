"""The main thread's topic tools (app/main_topics.py): topic_start in both tiers, its topic card and its launch card's
prefill, one topic per request, topic_send as a supplement or a next turn, and topic_fork."""
from __future__ import annotations

import itertools
import json
from types import SimpleNamespace
from unittest import mock

from support.fake_lark import FakeCardKit
from support.world import UnifiedCase

from support import requires_extension
from covey.app import main_topics
from covey.app.main_env import MainEnv
from covey.core import main_thread_settings
from covey.core.jobs import find_job_record, upsert_job_record
from covey.core.main_requests import Request
from covey.core.steer import SteerRefused
from covey.core.topics import load_topic

CHAT = "oc_owner_dm"


def data_harness(case, data):
    return load_topic(case.ctx.state_dir, data["topic_id"])["harness_id"]


class MainTopicsCase(UnifiedCase):
    def setUp(self):
        super().setUp()
        FakeCardKit().start(self)  # A CardKit request that goes around the mocks below fails the test.
        self.app.runtime.access.remember_owner_chat(CHAT)
        self.roots = itertools.count(1)
        self.app.messenger.create_and_send_card = mock.Mock(side_effect=self.sent_card)
        self.app.messenger.put_card = mock.Mock(return_value={"code": 0})
        self.env = MainEnv(runtime=self.app.runtime, topics=self.app.topics, messenger=self.app.messenger,
                           voice=self.app.voice, state_dir=self.ctx.state_dir, profile="test-bot")
        self.ids = itertools.count(1)

    def sent_card(self, profile, chat, card, key, **kwargs):
        number = next(self.roots)
        return kwargs.get("card_id") or f"card-{number}", f"om_root{number}"

    def call(self, tool, request_id=None, **args):
        request = Request(request_id or f"req{next(self.ids)}", tool, args, "main-job", 0.0)
        return main_topics.TOOLS[tool](self.env, request)

    def start(self, request_id=None, **args):
        fields = dict(title="查日志", prompt="看看昨晚的失败", workspace="project", readonly=True,
                      user_requested=False, why="日志在这个仓库")
        fields.update(args)
        return self.call("topic_start", request_id, **fields)

    def settings(self, **changes):
        main_thread_settings.update(self.ctx.state_dir, **changes)

    def job(self, answer):
        return self.app.runtime.jobs.find(0, "main-" + main_topics.request_token(SimpleNamespace(id=answer)))


class TopicStartTest(MainTopicsCase):
    def test_a_topic_that_starts_at_once_is_bound_and_its_first_turn_submitted(self):
        answer = self.start("r1", harness="codex", effort="high")
        self.assertTrue(answer["ok"], answer)
        data = answer["data"]
        self.assertTrue(data["started"])
        self.assertRegex(answer["text"], r"^已开 T\d+「查日志」：直接开工$")
        topic = load_topic(self.ctx.state_dir, data["topic_id"])
        codex = self.app.apps["codex"]
        default = codex.models.configured_context(codex.harness.context).model  # The Bridge's own default.
        self.assertEqual((topic["harness_id"], topic["model"], topic["effort"], topic["workspace_name"]),
                         ("codex", default, "high", "project"))
        self.assertTrue(topic["workspace_locked"] and topic["harness_locked"])
        self.assertEqual(topic["binding_revision"], 1)
        self.assertEqual(topic["origin"], {"kind": "main", "request": "r1", "job_key": "main-job",
                                           "why": "日志在这个仓库", "readonly": True, "user_requested": False,
                                           "title": "查日志", "at": topic["origin"]["at"]})
        self.assertEqual(topic["root_id"], data["root_message_id"])
        job = self.job("r1")
        self.assertEqual((job.user_text, job.topic_id, job.harness_id), ("看看昨晚的失败", data["topic_id"], "codex"))
        self.app.apps["codex"].submission.selection_card.assert_not_called()

    def test_the_topic_card_goes_to_the_top_level_and_is_redrawn_with_its_number(self):
        data = self.start()["data"]
        sent = self.app.messenger.create_and_send_card.call_args
        self.assertEqual(sent.args[1], CHAT)
        self.assertNotIn("reply_to_message_id", sent.kwargs)  # Top level, in no topic.
        first = json.dumps(sent.args[2], ensure_ascii=False)
        self.assertIn("查日志", first)
        self.assertNotIn(data["label"], first)
        # Only the title and one grey line of its configuration (the owner, 2026-11-06): no reason, no pointer.
        self.assertNotIn("日志在这个仓库", first)
        self.assertNotIn("进展看话题里的任务卡", first)
        self.assertIn("<font color='grey'>Codex", first)
        put = self.app.messenger.put_card.call_args
        self.assertEqual((put.args[1], put.kwargs["sequence"]), ("card-1", 1))
        self.assertEqual(put.args[2]["header"]["title"]["content"], f"{data['label']} · 查日志")

    @requires_extension()
    def test_a_topic_that_waits_gets_a_prefilled_launch_card_and_no_binding(self):
        from covey.app.launch import build_launch_card

        self.settings(start=())
        answer = self.start(harness="maso", readonly=False)
        self.assertTrue(answer["ok"], answer)
        self.assertFalse(answer["data"]["started"])
        self.assertTrue(answer["text"].endswith("：启动卡在话题里，等主人点开始"))
        topic = load_topic(self.ctx.state_dir, answer["data"]["topic_id"])
        self.assertNotIn("workspace_locked", topic)
        self.assertNotIn("model", topic)
        self.assertEqual(topic["harness_id"], "maso")
        app = self.app.apps["maso"]
        app.submission.selection_card.assert_called_once()
        ctx = app.submission.selection_card.call_args.args[0]
        self.assertEqual(ctx.topic_id, answer["data"]["topic_id"])
        record = find_job_record(self.ctx.state_dir, "main-" + main_topics.request_token(SimpleNamespace(id="req1")))
        self.assertEqual((record["status"], record["user_text"]), ("waiting_workspace", "看看昨晚的失败"))
        card = build_launch_card(app.topics, ctx)
        token = next(p.stem for p in (self.ctx.state_dir / "launch-forms").glob("*.json"))
        form = self.launch_form(token)
        self.assertEqual((form["selected_model"], form["form_defaults"]["workspace"]), ("maso:maso-model", "project"))
        self.assertIn("maso", json.dumps(card, ensure_ascii=False))

    def test_an_unknown_workspace_is_refused_with_the_names(self):
        answer = self.start(workspace="elsewhere")
        self.assertFalse(answer["ok"])
        self.assertIn("可选：project", answer["text"])
        self.app.messenger.create_and_send_card.assert_not_called()

    def test_a_model_is_named_by_its_id_or_its_menu_name_and_an_unknown_one_is_refused(self):
        data = self.start("named", harness="codex", model="codex model", effort="max")["data"]
        topic = load_topic(self.ctx.state_dir, data["topic_id"])
        self.assertEqual((topic["model"], topic["effort"]), ("codex-model", "max"))
        refused = self.start(harness="codex", model="gpt-0")
        self.assertIn("可选：codex-model", refused["text"])
        self.assertIn("可选：low、high", self.start(harness="codex", model="codex-model", effort="huge")["text"])

    def test_the_settings_defaults_fill_what_the_call_leaves_out(self):
        self.settings(topic_defaults=main_thread_settings.TopicDefaults(harness="codex", model="codex-model",
                                                                        effort="low", redaction=True))
        topic = load_topic(self.ctx.state_dir, self.start()["data"]["topic_id"])
        self.assertEqual((topic["harness_id"], topic["model"], topic["effort"]), ("codex", "codex-model", "low"))
        self.assertNotIn("redaction", topic)  # Codex cannot redact: the default does not apply to it.

    def test_with_no_defaults_a_topic_runs_as_the_chats_last_launch(self):
        from covey.app.launch import chat_defaults_path
        from covey.core.state import write_json
        menu = {"claude:opus": {"harness": "claude", "model": "opus", "levels": ["high", "max"]},
                "codex:gpt": {"harness": "codex", "model": "gpt", "levels": ["low", "high"]}}
        write_json(chat_defaults_path(self.ctx.state_dir, CHAT), {"chat_id": CHAT, "model_choice": "claude:opus",
                                                                  "effort": "max"})
        with mock.patch("covey.app.main_topics.available_models", return_value=menu):
            last = load_topic(self.ctx.state_dir, self.start()["data"]["topic_id"])
            named = load_topic(self.ctx.state_dir, self.start(harness="codex")["data"]["topic_id"])
            write_json(chat_defaults_path(self.ctx.state_dir, CHAT), {"chat_id": CHAT, "model_choice": "claude:opus",
                                                                      "effort": "xhigh"})
            fallback = self.start()
        self.assertEqual((last["harness_id"], last["model"], last["effort"]), ("claude", "opus", "max"))
        self.assertEqual(named["harness_id"], "codex")  # A Harness the call names: not the last launch's model.
        self.assertTrue(fallback["ok"], fallback)  # A budget the model no longer offers falls back, not refused.
        self.assertEqual(load_topic(self.ctx.state_dir, fallback["data"]["topic_id"])["harness_id"], "claude")

    @requires_extension()
    def test_a_maso_model_runs_as_the_preset_of_its_budget(self):
        menu = {"maso:m": {"harness": "maso", "model": "m", "levels": ["high", "max"], "recommended": "high",
                           "presets": {"high": "m-high", "max": "m-max"}}}
        with mock.patch("covey.app.main_topics.available_models", return_value=menu):
            named = self.start(harness="maso", model="m", effort="max")["data"]
            usual = self.start(harness="maso", model="m")["data"]
        topic = load_topic(self.ctx.state_dir, named["topic_id"])
        self.assertEqual((topic["model"], topic["effort"]), ("m-max", "max"))
        topic = load_topic(self.ctx.state_dir, usual["topic_id"])
        self.assertEqual((topic["model"], topic["effort"]), ("m-high", "high"))

    def test_the_reason_shows_literally_on_the_card(self):
        self.start(why="<at user_id=\"all\"></at>")
        self.assertNotIn("<at", json.dumps(self.app.messenger.create_and_send_card.call_args.args[2], ensure_ascii=False))

    def test_an_unknown_or_disabled_harness_is_refused(self):
        self.assertIn("没有 Harness「neeko」", self.start(harness="neeko")["text"])
        self.app.messenger.create_and_send_card.assert_not_called()

    def test_one_request_opens_one_topic(self):
        first = self.start("same")
        again = self.start("same")
        self.assertEqual(first, again)
        self.assertEqual(self.app.messenger.create_and_send_card.call_count, 1)
        self.assertEqual(self.app.messenger.put_card.call_count, 1)

    def test_a_second_try_after_the_card_was_created_sends_the_same_card(self):
        sent = self.app.messenger.create_and_send_card
        sent.side_effect = lambda *a, **k: (k["on_created"]("card-kept"), ("card-kept", ""))[1]
        self.assertFalse(self.start("again")["ok"])
        sent.side_effect = self.sent_card
        self.assertTrue(self.start("again")["ok"])
        self.assertEqual(sent.call_args.kwargs["card_id"], "card-kept")
        self.assertEqual(sent.call_args_list[0].args[3], sent.call_args.args[3])  # The same send key.

    def test_outside_the_owners_chat_nothing_runs(self):
        with mock.patch.object(type(self.app.runtime.access), "owner_chat", return_value=""):
            answer = self.start()
        self.assertFalse(answer["ok"])
        self.app.messenger.create_and_send_card.assert_not_called()


class TopicDefaultsTest(MainTopicsCase):
    """Each of Harness, model and budget on its own: the call, the settings, the chat's last launch, the Bridge."""
    MENU = {"claude:opus": {"harness": "claude", "model": "opus", "display_name": "Opus",
                            "levels": ["medium", "high", "xhigh", "max"], "recommended": "xhigh"},
            "claude:sonnet": {"harness": "claude", "model": "sonnet", "levels": ["low", "medium", "high"]},
            "codex:gpt": {"harness": "codex", "model": "gpt", "levels": ["low", "high"]}}

    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch("covey.app.main_topics.available_models", return_value=self.MENU))

    def launched(self, key, effort):
        from covey.app.launch import chat_defaults_path
        from covey.core.state import write_json
        write_json(chat_defaults_path(self.ctx.state_dir, CHAT), {"chat_id": CHAT, "model_choice": key, "effort": effort})

    def defaults(self, **fields):
        self.settings(topic_defaults=main_thread_settings.TopicDefaults(**fields))

    def opened(self, **args):
        answer = self.start(**args)
        self.assertTrue(answer["ok"], answer)
        topic = load_topic(self.ctx.state_dir, answer["data"]["topic_id"])
        return topic["harness_id"], topic["model"], topic["effort"]

    def resolved(self, **args):
        found = main_topics.resolve(self.app.topics, self.ctx.state_dir, CHAT,
                                    main_thread_settings.load(self.ctx.state_dir).topic_defaults, args)
        return found["harness"], found["model"], found["effort"], found["sources"]

    def own(self, name):
        app = self.app.apps[name]
        return app.models.configured_context(app.harness.context)

    def test_a_harness_alone_in_the_settings_takes_the_last_launchs_model_and_budget(self):
        # T405 (11-06): the settings named claude only; the owner had been launching opus · max.
        self.launched("claude:opus", "max")
        self.defaults(harness="claude")
        self.assertEqual(self.opened(), ("claude", "opus", "max"))
        self.assertEqual(self.resolved()[3], {"harness": "settings", "model": "last", "effort": "last"})

    def test_a_model_in_the_settings_takes_the_last_launchs_budget_when_the_model_offers_it(self):
        self.launched("claude:opus", "xhigh")
        self.defaults(harness="claude", model="opus")
        self.assertEqual(self.resolved(), ("claude", "opus", "xhigh",
                                           {"harness": "settings", "model": "settings", "effort": "last"}))
        self.launched("claude:sonnet", "low")  # opus has no low: the Bridge's budget.
        self.assertEqual(self.resolved()[2:], (self.own("claude").effort,
                                               {"harness": "settings", "model": "settings", "effort": "bridge"}))
        self.launched("claude:sonnet", "top_minus_one")  # 推荐 on the launch card: opus's recommended one.
        self.assertEqual(self.resolved()[2], "xhigh")

    def test_the_settings_budget_wins_over_the_last_launch_and_the_call_over_both(self):
        self.launched("claude:opus", "max")
        self.defaults(harness="claude", model="opus", effort="high")
        self.assertEqual(self.opened(), ("claude", "opus", "high"))
        self.assertEqual(self.opened(model="sonnet", effort="low"), ("claude", "sonnet", "low"))
        # The call's model with the settings' budget where that model offers it, else the last launch's.
        self.assertEqual(self.resolved(model="sonnet")[2:], ("high", {"harness": "settings", "model": "call",
                                                                     "effort": "settings"}))

    def test_another_harness_takes_neither_the_settings_nor_the_last_launchs_model(self):
        self.launched("claude:opus", "max")
        self.defaults(harness="codex")
        bridge = self.own("codex")
        self.assertEqual(self.resolved(), ("codex", bridge.model, bridge.effort,
                                           {"harness": "settings", "model": "bridge", "effort": "bridge"}))
        self.defaults(harness="claude", model="opus", effort="max")
        self.assertEqual(self.resolved(harness="codex", model="gpt")[1:3], ("gpt", bridge.effort))
        self.launched("codex:gpt", "low")
        self.assertEqual(self.resolved(harness="codex")[1:], ("gpt", "low", {"harness": "call", "model": "last",
                                                                              "effort": "last"}))

    def test_with_nothing_set_the_last_launch_then_the_bridge(self):
        self.assertEqual(self.resolved()[3], {"harness": "bridge", "model": "bridge", "effort": "bridge"})
        self.launched("claude:gone", "max")  # A model no longer on the menu: the Bridge's model.
        name, model, _, sources = self.resolved()
        self.assertEqual((name, model, sources["model"]), ("claude", self.own("claude").model, "bridge"))
        self.launched("claude:opus", "default")  # The model's own default budget.
        self.assertEqual(self.resolved(), ("claude", "opus", "", {"harness": "last", "model": "last", "effort": "last"}))

    def test_a_budget_the_call_names_and_the_model_lacks_is_refused_one_from_elsewhere_falls_through(self):
        self.defaults(harness="claude", model="sonnet", effort="max")  # Hand-edited: sonnet has no max.
        self.assertEqual(self.resolved()[2:], (self.own("claude").effort,
                                               {"harness": "settings", "model": "settings", "effort": "bridge"}))
        refused = self.start(effort="max")
        self.assertFalse(refused["ok"])
        self.assertIn("没有思考预算「max」", refused["text"])


class TopicSendTest(MainTopicsCase):
    def opened(self):
        data = self.start("open")["data"]
        return data, self.job("open")

    def test_a_running_turn_takes_it_as_a_supplement(self):
        data, job = self.opened()
        job.execution_started = True
        job.backend_control = SimpleNamespace(steer=mock.Mock(return_value=None), remote=False)
        answer = self.call("topic_send", topic=data["label"], text="顺便看下前天的", note="主人说的是同一批任务")
        self.assertTrue(answer["ok"], answer)
        self.assertTrue(answer["data"]["steered"])
        sent = job.backend_control.steer.call_args.args[0]
        self.assertTrue(sent.startswith("[Bridge 说明] 主线程转来主人的补充"), sent)
        self.assertIn("任务说明（主线程整理）：\n顺便看下前天的\n主线程补充：主人说的是同一批任务", sent)
        # No record of the main-thread turn: the thread shows the main thread's words, never its note.
        self.assertIn("主线程转来你的补充：\n顺便看下前天的", self.said())
        # The note stays in the hand-over's record: the main thread's card names it (app/main_reply.py opened_lines).
        records = [json.loads(path.read_text()) for path in (self.ctx.state_dir / "main-thread" / "opened").glob("*.json")]
        self.assertIn("主人说的是同一批任务", [record.get("note") for record in records])

    def test_a_refused_supplement_is_said_and_not_sent_again(self):
        data, job = self.opened()
        job.execution_started = True
        job.backend_control = SimpleNamespace(steer=mock.Mock(side_effect=SteerRefused("任务正在收尾")), remote=False)
        answer = self.call("topic_send", topic=data["topic_id"], text="再加一条")
        self.assertFalse(answer["ok"])
        self.assertIn("任务正在收尾", answer["text"])
        self.assertEqual(job.backend_control.steer.call_count, 1)
        self.assertFalse(any("主线程转来" in text for text in self.said()))

    def test_a_stopped_topic_gets_its_next_turn(self):
        data, job = self.opened()
        self.app.runtime.unregister_reply_job(job)
        upsert_job_record(self.ctx.state_dir, job.message_id, status="done")
        answer = self.call("topic_send", "send-1", topic=data["label"].lower(), text="再跑一次", note="")
        self.assertTrue(answer["ok"], answer)
        self.assertFalse(answer["data"]["steered"])
        self.assertIn("主线程转来你的消息：\n再跑一次", self.said())
        nxt = self.job("send-1")
        self.assertEqual((nxt.user_text, nxt.topic_id), ("再跑一次", data["topic_id"]))
        self.assertEqual(nxt.title, "查日志")  # The topic's title heads its card.

    def test_a_stopped_topic_with_an_open_question_takes_it_as_a_topic_message_would(self):
        data, job = self.opened()
        self.app.runtime.unregister_reply_job(job)
        upsert_job_record(self.ctx.state_dir, job.message_id, status="waiting_user")
        app = self.app.apps[data_harness(self, data)]
        with mock.patch.object(app.questions, "pending_questions", return_value=[{"token": "t"}]), \
                mock.patch.object(app, "workspace_command", return_value=True) as command:
            answer = self.call("topic_send", "send-q", topic=data["label"], text="选第二个")
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(command.call_args.args[1:3], ("选第二个", CHAT))
        self.assertIsNone(self.job("send-q"))  # No next turn beside the question.

    def test_a_topic_whose_result_has_not_arrived_takes_nothing(self):
        data, job = self.opened()
        self.app.runtime.unregister_reply_job(job)
        upsert_job_record(self.ctx.state_dir, job.message_id, status="done")
        app = self.app.apps[data_harness(self, data)]
        with mock.patch.object(app, "workspace_command", return_value=True):
            answer = self.call("topic_send", "send-u", topic=data["label"], text="再跑一次")
        self.assertFalse(answer["ok"])
        self.assertIsNone(self.job("send-u"))

    def test_a_topic_waiting_on_its_launch_card_is_not_sent_to(self):
        self.settings(start=())
        data = self.start(readonly=False)["data"]
        answer = self.call("topic_send", topic=data["label"], text="再加一条")
        self.assertFalse(answer["ok"])
        self.assertIn("启动卡", answer["text"])

    def test_an_unknown_topic_is_refused(self):
        self.assertIn("没有找到话题 T999", self.call("topic_send", topic="T999", text="x")["text"])


class TopicForkTest(MainTopicsCase):
    def finished(self):
        data = self.start("open")["data"]
        job = self.job("open")
        self.app.runtime.unregister_reply_job(job)
        upsert_job_record(self.ctx.state_dir, job.message_id, status="done")
        return data

    def fork(self, data, request_id="fork-1"):
        with mock.patch("covey.feishu.message_read.fetch_message_detail", return_value={}), \
                mock.patch("covey.feishu.message_read.message_item", return_value={}):
            return self.call("topic_fork", request_id, topic=data["label"], title="换个思路", prompt="试试另一种做法")

    def test_a_fork_opens_a_topic_from_the_last_finished_turn_and_submits_its_first(self):
        data = self.finished()
        answer = self.fork(data)
        self.assertTrue(answer["ok"], answer)
        fork = answer["data"]
        self.assertNotEqual(fork["topic_id"], data["topic_id"])
        topic = load_topic(self.ctx.state_dir, fork["topic_id"])
        self.assertEqual(topic["forked_from"]["topic_id"], data["topic_id"])
        self.assertEqual((topic["origin"]["kind"], topic["origin"]["request"]), ("main", "fork-1"))
        job = self.job("fork-1")
        self.assertEqual((job.user_text, job.topic_id), ("试试另一种做法", fork["topic_id"]))
        self.assertEqual(self.fork(data), answer)  # The same request again: the same fork.

    def test_a_running_source_is_refused(self):
        data = self.start("open")["data"]
        answer = self.fork(data)
        self.assertFalse(answer["ok"])
        self.assertIn("topic_send", answer["text"])


class HandoffTest(MainTopicsCase):
    """What a topic gets from the main thread (app/main_handoff.py): the Bridge's note, the main thread's task, the
    owner's words from the main-thread turn's record and its attachments carried into the topic; its card, history
    and thread show the owner's words, never the note or a local path."""

    WORDS = "另开一个话题区：看看昨晚的失败"
    AT = "2026-11-08T05:45:00Z"

    def setUp(self):
        super().setUp()
        from covey.core.main_thread import main_topic_id
        from covey.core.attachments import ATTACHMENTS_HEADER
        media = self.ctx.state_dir / "topics" / main_topic_id(CHAT) / "incoming_media"
        media.mkdir(parents=True)
        self.image = media / "om_owner-1.png"
        self.image.write_bytes(b"\x89PNG fake")
        self.doc = media / "om_owner-2-notes.txt"
        self.doc.write_text("x")
        prompt = "\n\n".join((self.WORDS, ATTACHMENTS_HEADER, f"- 图片 1: `{self.image}`",
                               f"- 文件 1（notes.txt）: `{self.doc}`", "请在需要理解图片或文件内容时读取上述本地路径。"))
        upsert_job_record(self.ctx.state_dir, "main-job", job_id=7, user_text=self.WORDS + "\n附件：图片 1 张、文件 1 个（notes.txt）",
                          prompt_text=prompt, created_at=self.AT, chat_id=CHAT)

    def clock(self):
        import time
        from covey.core.state import epoch_seconds
        return time.strftime("%H:%M", time.localtime(epoch_seconds(self.AT)))

    def topic_media(self, topic_id):
        return self.ctx.state_dir / "topics" / topic_id / "incoming_media"

    def test_a_new_topic_gets_the_note_the_task_and_the_owners_words_its_card_only_the_words(self):
        from covey.core.attachments import native_image_attachments
        data = self.start("h1")["data"]
        job = self.job("h1")
        sent = job.prompt_text
        self.assertTrue(sent.startswith(f"[Bridge 说明] 你在话题区 {data['label']}「查日志」。"), sent)
        self.assertIn("不要再试图开话题或切话题", sent)
        self.assertIn("由 Bridge 发给主人", sent)
        self.assertIn("任务说明（主线程整理）：\n看看昨晚的失败", sent)
        self.assertIn(f"主人原话（{self.clock()}）：\n> {self.WORDS}", sent)
        # The attachments, carried into the topic's own folder, in backquotes: its model gets the image natively.
        carried = self.topic_media(data["topic_id"]) / self.image.name
        self.assertTrue(carried.is_file())
        self.assertIn(f"- 图片 1: `{carried}`", sent)
        self.assertIn(f"- 文件 1（notes.txt）: `{self.topic_media(data['topic_id']) / self.doc.name}`", sent)
        self.assertEqual([item["path"] for item in native_image_attachments(sent, self.topic_media(data["topic_id"]))],
                         [str(carried.resolve())])
        self.assertNotIn(str(self.image), sent)  # Not the main thread's copy.
        # The card and the history: the owner's words and the attachments in words, no path, no note.
        self.assertEqual(job.user_text, self.WORDS + "\n附件：图片 1 张、文件 1 个")
        history = [json.loads(line) for line in job.history_path.read_text().splitlines()]
        self.assertEqual(history[-1]["content"], job.user_text)

    def test_attachments_none_or_by_number_hands_on_those_only(self):
        self.start("h2", attachments="none")
        sent = self.job("h2").prompt_text
        self.assertNotIn("收到的附件", sent)
        self.assertEqual(self.job("h2").user_text, self.WORDS)
        self.start("h3", attachments=["文件 1"])
        sent = self.job("h3").prompt_text
        self.assertIn(self.doc.name, sent)
        self.assertNotIn(self.image.name, sent)
        self.assertFalse(self.start("h4", attachments="some")["ok"])

    def test_a_second_try_copies_nothing_again(self):
        data = self.start("h5")["data"]
        carried = self.topic_media(data["topic_id"]) / self.image.name
        before = carried.stat().st_ino
        from covey.app import main_handoff
        owner = main_handoff.asked(self.ctx.state_dir, "main-job")
        again = main_handoff.carry(owner.attachments, self.topic_media(data["topic_id"]), owner.roots)
        self.assertEqual([item.path for item in again][0], str(carried))
        self.assertEqual(carried.stat().st_ino, before)
        self.assertEqual(sorted(p.name for p in self.topic_media(data["topic_id"]).iterdir()),
                         sorted((self.image.name, self.doc.name)))

    def test_a_supplement_shows_the_owners_words_in_the_thread_and_sends_the_note(self):
        data = self.start("open")["data"]
        job = self.job("open")
        job.execution_started = True
        job.backend_control = SimpleNamespace(steer=mock.Mock(return_value=None), remote=False)
        before = len(self.said())
        answer = self.call("topic_send", "s1", topic=data["label"], text="看看昨晚的失败", note="截图是任务卡")
        self.assertTrue(answer["ok"], answer)
        sent = job.backend_control.steer.call_args.args[0]
        self.assertTrue(sent.startswith(f"[Bridge 说明] 主线程转来主人 {self.clock()} 的补充"), sent)
        self.assertIn(f"你仍在 {data['label']}「查日志」的这一轮里", sent)
        self.assertIn("主线程补充：截图是任务卡", sent)
        line = self.said()[before:]
        self.assertEqual(line, [f"主线程转来你 {self.clock()} 的补充：\n{self.WORDS}\n附件：图片 1 张、文件 1 个"])
        self.assertNotIn("截图是任务卡", line[0])
        # The card's 最新补充 and the topic's history: the owner's words, not the note.
        from covey.core.deliveries import delivery_path, read_delivery, shown_text
        mid = "main-" + main_topics.request_token(SimpleNamespace(id="s1"))
        record = read_delivery(delivery_path(self.ctx.state_dir, data["topic_id"], mid))
        self.assertEqual(shown_text(record), self.WORDS + "\n附件：图片 1 张、文件 1 个")
        self.assertEqual(record["text"], sent)
        history = [json.loads(row) for row in job.history_path.read_text().splitlines()]
        self.assertEqual(history[-1]["content"], shown_text(record))

    def test_a_next_turn_gets_the_next_note_and_the_topics_title(self):
        data = self.start("open")["data"]
        job = self.job("open")
        self.app.runtime.unregister_reply_job(job)
        upsert_job_record(self.ctx.state_dir, job.message_id, status="done")
        answer = self.call("topic_send", "n1", topic=data["label"], text="再跑一次", attachments="none")
        self.assertTrue(answer["ok"], answer)
        nxt = self.job("n1")
        self.assertTrue(nxt.prompt_text.startswith(f"[Bridge 说明] 你在话题区 {data['label']}「查日志」。主线程把主人"),
                        nxt.prompt_text)
        self.assertIn("下一轮", nxt.prompt_text)
        self.assertEqual((nxt.user_text, nxt.title), (self.WORDS, "查日志"))
        self.assertIn(f"主线程转来你 {self.clock()} 的消息：\n{self.WORDS}", self.said())

    def test_the_line_in_the_topics_thread_opens_the_owners_message_in_the_chat(self):
        # The owner, 2026-11-10 17:59: 话题里那条主线程转来的消息，点了跳回聊天区原来那条 (it was text, no link).
        from covey.app import answer_cards
        upsert_job_record(self.ctx.state_dir, "main-job", message_id="om_owner")
        data = self.start("open")["data"]
        job = self.job("open")
        self.app.runtime.unregister_reply_job(job)
        upsert_job_record(self.ctx.state_dir, job.message_id, status="done")

        def send(profile, chat, text, key, **kwargs):
            if kwargs.get("card") is not None:
                kwargs["on_card"]("om_line")
            return True
        self.app.messenger.send_message.side_effect = send
        place = "https://applink.feishu.cn/client/chat/open?openChatId=oc&position=7"
        with mock.patch.object(answer_cards, "ask_link", return_value=place) as link:
            self.assertTrue(self.call("topic_send", "n1", topic=data["label"], text="再跑一次", attachments="none")["ok"])
        self.assertEqual((link.call_args.args[3], link.call_args.kwargs["chat_place"]), ("om_owner", True))
        call = next(c for c in self.app.messenger.send_message.call_args_list if c.kwargs.get("card") is not None)
        line = f"主线程转来你 {self.clock()} 的消息"
        self.assertEqual(call.args[2], f"{line}：\n{self.WORDS}")  # The text, as before, when the card is refused.
        box = call.kwargs["card"]["body"]["elements"][0]
        self.assertEqual((box["tag"], box["behaviors"]), ("interactive_container",
                                                          [{"type": "open_url", "default_url": place}]))
        shown = json.dumps(box, ensure_ascii=False)
        self.assertIn(f"{line}　›", shown)
        self.assertIn(self.WORDS, shown)
        self.assertEqual(call.kwargs["card"]["config"]["summary"]["content"], line)
        records = [json.loads(path.read_text()) for path in (self.ctx.state_dir / "main-thread" / "opened").glob("*.json")]
        self.assertIn("om_line", [record.get("thread_message_id") for record in records])

    def test_with_the_owners_message_out_of_reach_the_line_is_text_as_before(self):
        data = self.start("open")["data"]
        job = self.job("open")
        self.app.runtime.unregister_reply_job(job)
        upsert_job_record(self.ctx.state_dir, job.message_id, status="done")
        self.assertTrue(self.call("topic_send", "n1", topic=data["label"], text="再跑一次", attachments="none")["ok"])
        sent = [c for c in self.app.messenger.send_message.call_args_list if "主线程转来" in c.args[2]]
        self.assertEqual([c.kwargs.get("card") for c in sent], [None])

    def test_a_fork_says_where_it_came_from(self):
        data = self.start("open")["data"]
        job = self.job("open")
        self.app.runtime.unregister_reply_job(job)
        upsert_job_record(self.ctx.state_dir, job.message_id, status="done")
        with mock.patch("covey.feishu.message_read.fetch_message_detail", return_value={}), \
                mock.patch("covey.feishu.message_read.message_item", return_value={}):
            fork = self.call("topic_fork", "f1", topic=data["label"], title="换个思路", prompt="试试另一种做法")["data"]
        sent = self.job("f1").prompt_text
        self.assertIn(f"从 {data['label']} 分叉出这个话题区", sent)
        self.assertIn(f"你在话题区 {fork['label']}「换个思路」", sent)
        self.assertTrue((self.topic_media(fork["topic_id"]) / self.image.name).is_file())

    def test_supplements_merged_before_the_turn_started_are_the_owners_words_too(self):
        from covey.app import main_handoff
        upsert_job_record(self.ctx.state_dir, "main-job-2", job_id=8, user_text="附件：图片 1 张", chat_id=CHAT,
                          prompt_text=f"收到的附件（已下载到本地）：\n\n- 图片 1: `{self.image}`"
                                      + main_handoff.MERGED + "把这张图里的报错查一下\n\n回复上下文：\n- parent_id: `om_x`")
        owner = main_handoff.asked(self.ctx.state_dir, "main-job-2")
        self.assertEqual(owner.words, "把这张图里的报错查一下")
        self.assertEqual([item.path for item in owner.attachments], [str(self.image)])


    def supplement(self, mid, words, *, status="sent", after=120, image=None, **fields):
        """A supplement the main-thread turn "main-job" took while it ran (app/main_thread.py supplement)."""
        from covey.core.deliveries import delivery_path, save_delivery
        from covey.core.main_thread import main_topic_id
        from covey.core.state import epoch_seconds
        from covey.core.attachments import ATTACHMENTS_HEADER
        text = words + (f"\n\n{ATTACHMENTS_HEADER}\n- 图片 1: `{image}`" if image else "")
        save_delivery(delivery_path(self.ctx.state_dir, main_topic_id(CHAT), mid),
                      {"status": status, "text": text, "topic_id": main_topic_id(CHAT), "chat_id": CHAT, "job_id": 7,
                       "job_key": "main-job", "mid": mid, "saved_at": epoch_seconds(self.AT) + after, **fields})

    def test_what_the_owner_added_while_the_main_thread_ran_is_handed_on_with_its_image(self):
        # A1 review: the owner sent B ("这张图交给 T444" and a picture) while the main thread answered A; the topic got
        # A as the owner's words and not B's picture.
        import time
        from covey.core.state import epoch_seconds
        later = self.image.with_name("om_owner-3.png")
        later.write_bytes(b"\x89PNG later")
        self.supplement("om_b", "这张图也交给它", image=later)
        self.supplement("om_c", "被拒的那条", status="refused", after=150)
        self.supplement("om_d", "开工前并进去的", after=10, merged=True)
        data = self.start("h6", attachments=["图片 2"])["data"]
        job = self.job("h6")
        sent = job.prompt_text
        b_clock = time.strftime("%H:%M", time.localtime(epoch_seconds(self.AT) + 120))
        self.assertIn(f"主人原话（{self.clock()}）：\n> {self.WORDS}\n主人随后补充（{b_clock}）：\n> 这张图也交给它", sent)
        self.assertNotIn("被拒的那条", sent)
        self.assertNotIn("开工前并进去的", sent)  # In the turn's request already: its words come from there.
        # Numbered after the first message's: B's picture is 图片 2.
        carried = self.topic_media(data["topic_id"]) / later.name
        self.assertTrue(carried.is_file())
        self.assertIn(f"- 图片 1: `{carried}`", sent)
        self.assertNotIn(self.image.name, sent)
        self.assertEqual(job.user_text, f"{self.WORDS}\n这张图也交给它\n附件：图片 1 张")
        self.assertIn(f"主线程按主人 {self.clock()}、{b_clock} 在", sent)

    def test_only_files_saved_in_the_main_threads_media_are_carried(self):
        # A1 review: a forwarded message's text goes into the prompt as it is, so a block it carries could name any
        # file on the machine; only what the Bridge saved for the main thread is handed on.
        from covey.app import main_handoff
        from covey.core.attachments import ATTACHMENTS_HEADER
        outside = self.ctx.state_dir / "secret.png"
        outside.write_bytes(b"\x89PNG secret")
        link = self.image.with_name("om_owner-9.png")
        link.symlink_to(outside)
        prompt = "\n".join(("转发的消息", ATTACHMENTS_HEADER, f"- 图片 1: `{outside}`", f"- 图片 2: `{link}`",
                             f"- 图片 3: `{self.image}`"))
        upsert_job_record(self.ctx.state_dir, "main-job-4", job_id=9, user_text="转发的消息", prompt_text=prompt,
                          created_at=self.AT, chat_id=CHAT)
        owner = main_handoff.asked(self.ctx.state_dir, "main-job-4")
        self.assertEqual([item.path for item in owner.attachments], [str(self.image)])
        target = self.ctx.state_dir / "topics" / "elsewhere" / "incoming_media"
        listed = main_handoff.parse_attachment_block(prompt)
        self.assertEqual([item.path for item in main_handoff.carry(listed, target, owner.roots)],
                         [str(target / self.image.name)])
        self.assertFalse((target / outside.name).exists())
        self.assertFalse((target / link.name).exists())

class AttachmentBlockTest(UnifiedCase):
    def test_the_block_a_message_is_enriched_with_reads_back(self):
        from pathlib import Path
        from covey.core.attachments import (ListedAttachment, attachment_block, message_words,
                                                     parse_attachment_block)
        from covey.feishu.media import _attachment_lines
        from covey.feishu.message_read import IncomingMedia
        media = [IncomingMedia("img_1", "image", Path("/a/1.png")), IncomingMedia("file_1", "file", Path("/a/r.pdf")),
                 IncomingMedia("img_2", "image", Path("/a/2.png"))]
        text = "\n\n".join(["看下这几个", *_attachment_lines(media, media, [], {"file_1": "r.pdf"})])
        listed = parse_attachment_block(text)
        self.assertEqual(listed, [ListedAttachment("image", "", "/a/1.png"), ListedAttachment("file", "r.pdf", "/a/r.pdf"),
                                  ListedAttachment("image", "", "/a/2.png")])
        self.assertEqual("\n".join(attachment_block(listed)), "\n".join(_attachment_lines(media, media, [], {"file_1": "r.pdf"})))
        self.assertEqual(message_words(text), "看下这几个")

    def test_the_words_leave_out_the_mentions_and_their_open_ids(self):
        # S7 review: an @ in the private chat put its open_id in the "你问" block and the words handed to a topic.
        import json
        import tempfile
        from pathlib import Path
        from covey.core.attachments import message_words
        from covey.feishu.media import enrich_incoming_event
        event = {"message_id": "om_1", "message_type": "text", "chat_id": "oc_chat", "create_time": "1700000000",
                 "content": json.dumps({"text": "@_user_1 看下这个"}, ensure_ascii=False),
                 "mentions": [{"id": "ou_fake_zhang", "key": "@_user_1", "name": "张三"}]}
        with tempfile.TemporaryDirectory() as media:
            text = enrich_incoming_event(event, Path(media), message_detail={"ok": False}).text
        self.assertIn("提及信息：", text)
        self.assertIn("ou_fake_zhang", text)
        words = message_words(text)
        self.assertIn("看下这个", words)
        self.assertNotIn("ou_fake_zhang", words)
        self.assertNotIn("提及信息", words)
