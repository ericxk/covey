"""Bridge apps in a temporary state directory, driven through their public entry points.

- ``TopicFixture``: one Claude BridgeApp with a recording backend (mixin).
- ``QuestionFixture``: a TopicFixture with a running task waiting on one question.
- ``UnifiedCase``: the UnifiedBridgeApp with every Harness, as the live Bridge runs.
- ``LaunchCase``, ``LaunchCardCase``, ``PickerCase``: launch card, published launch
  card and directory picker flows on top of ``UnifiedCase``.

Every app logs into ``self.logs`` instead of stdout. Before a fixture's temporary directory goes,
``stop_background`` stops its apps' polling threads and fails the test if one keeps running.
"""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support import REPO, make_context, quiet
from covey.core.topics import context_for_topic
from support.cards import current_token, fill
from support.clock import CountingShutdown
from support.fake_backends import RecordingBackend
from support.fake_lark import FakeCardKit, route_launch_receipts
from support.processes import Leftovers


def harness_app(spec, backend, store, *, runtime=None):
    """One Harness's BridgeApp on a fresh Runtime (or a given one) and its own Messenger, as the
    Bridge builds each of its apps, without the Bridge around it (``app.topics`` is None)."""
    from covey.app.app import BridgeApp
    from covey.app.harnesses import Harness
    from covey.app.runtime import Runtime
    from covey.feishu.messenger import Messenger

    runtime = runtime or Runtime(script_path="unit")
    return BridgeApp(Harness(spec, backend, store), runtime=runtime, messenger=Messenger(runtime.log))


def build_claude_app(backend=None):
    from covey.backends.claude.bridge import CLAUDE_SPEC, ClaudeBackend
    from covey.backends.claude.sessions import ClaudeSessionStore

    app = harness_app(CLAUDE_SPEC, backend or ClaudeBackend(), ClaudeSessionStore())
    quiet(app)
    return app


def stop_background(test, apps, leftovers, timeout=2.0):
    """A fixture's cleanup, run before its temporary directory goes: set each app's shutdown, so its
    polling loops (receipt watchers, held supplements, card action workers) end, and wait for the threads
    the test started. One left running writes into the removed directory while later tests run."""
    from covey.app.card_queue import CardActionQueue

    for app in apps():
        app.runtime.shutdown.set()
        for queue in vars(app).values():
            if isinstance(queue, CardActionQueue) and queue.started:
                for _ in range(queue.workers):  # An idle worker sees the shutdown now, not after its poll.
                    queue.submit("", lambda: None)
    if left := leftovers.threads(timeout):
        test.fail(f"threads still running {timeout} s after the test: {sorted({thread.name for thread in left})}")


def acknowledging_lanes(test, handle, lane="topic:t"):
    """Card lanes running ``handle`` as the Bridge runs clicks, all in one topic: (lanes, acknowledged),
    where ``acknowledged`` gets each click's event_id once its handler returned (the click's ack)."""
    from covey.app.card_lanes import CardLanes

    shutdown, acknowledged = threading.Event(), []
    test.addCleanup(shutdown.set)

    def handled(event, ctx):
        handle(event, ctx)
        acknowledged.append(event.get("event_id"))
    return CardLanes(handled, lambda event, ctx: lane, lambda line: None, shutdown), acknowledged


def wait_for(test, predicate, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end and not predicate():
        time.sleep(0.01)
    test.assertTrue(predicate())


class TopicFixture:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(stop_background, self, lambda: [self.app], Leftovers())
        self.root = Path(self.tmp.name)
        # As the Bridge builds a Harness's context: its topics are bound to this Harness.
        self.ctx = make_context(self.root, options={"harness_id": "claude"})
        self.backend = RecordingBackend()
        self.app = build_claude_app(self.backend)
        self.logs = quiet(self.app)
        self.app.cards.streaming = False
        self.app.messenger.send_message = mock.Mock(return_value=True)
        self.app.runtime.owner_open_id = "ou_user"

    def topic(self, root, chat="oc_chat", **extra):
        return context_for_topic(self.ctx, {"chat_id": chat, "message_id": root, **extra}, lambda _: self.root)

    def receive(self, message, content, root="", thread=""):
        self.app.handle_event({"chat_id": "oc_chat", "sender_id": "ou_user", "message_id": message,
                             "content": content, "root_id": root, "thread_id": thread}, self.ctx, fetch_message_details=False)

    def work(self):
        job = self.app.runtime.jobs.queue.get_nowait()
        self.app.process_reply_job(job, self.ctx)
        return job

    def fast_polling(self):
        """Give the app a shutdown event whose polling waits last one short tick and are
        counted (CountingShutdown): held supplements, receipt watchers and the like run
        their rounds at once, and a test can wait for rounds instead of seconds."""
        self.app.runtime.shutdown = self.shutdown = CountingShutdown()
        self.addCleanup(self.shutdown.set)
        return self.shutdown


class QuestionFixture(TopicFixture):
    """A received task registered as running, with one pending blocking question (request 17)."""

    def setUp(self):
        super().setUp()
        self.receive("om_question", "task")
        self.job = self.app.runtime.jobs.queue.get_nowait()
        self.ctx_topic = self.topic("om_question")
        self.rpc = SimpleNamespace(answer=mock.Mock(), steer=mock.Mock(), user_requests={17})
        self.job.backend_control = self.rpc
        self.job.checkpoint_path = str(self.root / "checkpoint.json")
        Path(self.job.checkpoint_path).write_text('{"requires_user":true}')
        self.app.runtime.jobs.register(self.job)
        self.app.cards.set_task_element = mock.Mock(return_value=True)
        self.token = "a" * 16
        self.path = self.ctx_topic.state_dir / "questions" / (self.token + ".json")
        self.path.parent.mkdir(parents=True)
        self.record = {
            "token": self.token,
            "job_id": self.job.job_id,
            "topic_id": self.job.topic_id,
            "chat_id": "oc_chat",
            "status": "pending",
            "blocking": True,
            "request_id": 17,
            "created_at": 1,
            "questions": [
                {
                    "id": "scope",
                    "question": "范围？",
                    "header": "范围",
                    "options": [{"label": "全部", "description": "处理全部数据"}],
                }
            ],
        }
        self.path.write_text(json.dumps(self.record))


class UnifiedCase(unittest.TestCase):
    """The UnifiedBridgeApp of a live Bridge with outbound sends mocked."""

    def setUp(self):
        from covey.core.state import write_private_text

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.built = []  # Every UnifiedBridgeApp bridge() made: one per restart.
        self.addCleanup(stop_background, self, lambda: self.built, Leftovers())
        self.root = Path(self.tmp.name)
        self.work = self.root / 'project'
        self.work.mkdir()
        self.logs = []
        self.app, self.ctx = self.bridge()
        for name in self.app.apps:
            write_private_text(self.ctx.state_dir / 'launch-models' / (name + '.json'), json.dumps({
                'updated_at': time.time(), 'models': {name + '-model': {'display_name': name + ' model',
                           'levels': ['low', 'high', 'xhigh', 'max'], 'default': 'high'}}}))
        self.app.messenger.send_message = mock.Mock(return_value=True)
        self.app.messenger.recall = mock.Mock(return_value=True)  # The bot menu's Panels: never a real recall.
        # A quiet butler turn's reactions (app/butler_reactions.py): never a real one.
        self.app.messenger.react = mock.Mock(return_value='reaction-1')
        self.app.messenger.unreact = mock.Mock(return_value=True)
        self.app.messenger.reaction_ids = mock.Mock(return_value=[])
        for app in self.app.apps.values():
            app.submission.selection_card = mock.Mock()
            app.cards.create_streaming_reply = mock.Mock(return_value=None)
            app.harness.started = True
            app.models.prepare_submission_context = lambda ctx: ctx

    def bridge(self):
        """A UnifiedBridgeApp over this state directory, paired with ``owner`` and logging
        into ``self.logs``. Called again it is the Bridge after a restart (nothing mocked)."""
        from covey.app.unified import UnifiedBridgeApp

        app = UnifiedBridgeApp(script_path=REPO / 'bin/covey_bridge.py')
        args = app.build_parser().parse_args(['--workdir', str(self.work), '--state-dir', str(self.root / 'state'),
                                              '--profile', 'test-bot'])
        ctx = app.build_context(args)
        quiet(app, self.logs)
        app.runtime.owner_open_id = 'owner'
        self.built.append(app)
        return app, ctx

    def event(self, mid, content='task', root=''):
        return {'chat_id': 'chat', 'sender_id': 'owner', 'message_id': mid, 'root_id': root, 'content': content}

    def route(self, mid):
        return self.app.topics.route(self.ctx, self.event(mid))

    def fast_polling(self):
        """TopicFixture.fast_polling for the Bridge and every routed Harness app."""
        self.shutdown = CountingShutdown()
        for app in {id(a): a for a in (self.app, *self.app.apps.values())}.values():
            app.runtime.shutdown = self.shutdown
        self.addCleanup(self.shutdown.set)
        return self.shutdown

    def receive(self, mid, content, root='', display=None):
        with mock.patch('covey.app.app.enrich_incoming_event') as enrich:
            event = self.event(mid, content, root)
            enrich.return_value = type('Message', (), {'text': content, 'display_text': display or content, 'event': event})()
            self.app.handle_event(event, self.ctx)

    def said(self):
        """The text of every message the Bridge sent, in order."""
        return [c.args[2] for c in self.app.messenger.send_message.call_args_list]

    def notices(self):
        """said() without the notice that a queued job has no task card: this fixture's
        create_streaming_reply returns None while streaming cards are on."""
        return [text for text in self.said() if '任务卡没有发出去' not in text]

    def launch_form(self, token):
        """The saved record of launch form ``token``."""
        return json.loads((self.ctx.state_dir / 'launch-forms' / (token + '.json')).read_text())

    def remembered_launch_card(self, mid='task', **remember):
        """A message waiting for its launch card, and that card remembered as sent (card
        ``cid``, message ``mid``) without CardKit. Sets ``child`` and ``target``; returns
        the card, its token and its record path."""
        from covey.app.launch import build_launch_card, card_token, remember_launch_card

        self.receive(mid, 'pending')
        self.child, self.target = self.route(mid)
        card = build_launch_card(self.child.topics, self.target)
        remember_launch_card(self.target, card, 'cid', 'mid', **remember)
        token = card_token(card)
        return card, token, self.ctx.state_dir / 'launch-forms' / (token + '.json')


class LaunchCase(UnifiedCase):
    """Launch cards built and remembered directly, submitted through the card callbacks."""

    def setUp(self):
        super().setUp()
        FakeCardKit().start(self)

    def form(self, mid, *, new_topic=False):
        from covey.app.launch import SUBMIT_PREFIX, build_launch_card, card_token, remember_launch_card
        from covey.core.topics import remember_topic_message

        app, ctx = self.route(mid)
        card = build_launch_card(app.topics, ctx, new_topic=new_topic)
        action = SUBMIT_PREFIX + card_token(card)
        remember_topic_message(ctx.state_dir, ctx.topic_id, 'chat', 'card-' + mid)
        remember_launch_card(ctx, card, 'card-id-' + mid, 'card-' + mid)
        return card, {'operator_id': 'owner', 'chat_id': 'chat', 'message_id': 'card-' + mid,
                      'event_id': 'click-' + mid, 'action_tag': 'button', 'action_name': action,
                      'form_value': {'model': 'maso:maso-model', 'effort': 'high', 'workspace': ctx.workspace_name, 'path': '',
                                     'run_mode': 'direct'}}

    def submit(self, event):
        self.app.handle_card_action_event(self.choose_model(event), self.ctx)

    def choose_model(self, event):
        """The select callbacks a person makes before the start click: model, budget,
        workspace and run mode. Returns the click the card shows after them (each choice
        redraws the card on a new token); the form itself sends only the path."""
        from covey.app.launch import SUBMIT_PREFIX

        fields = event.get('form_value')
        if isinstance(fields, dict) and fields.get('model'):
            token = event['action_name'][len(SUBMIT_PREFIX):]

            def choose(kind, option):
                nonlocal token
                with FakeCardKit():
                    self.app.handle_card_action_event({**event, 'action_name': '', 'event_id': kind + '-' + str(option),
                        'action_value': {'kind': kind, 'token': token}, 'option': option}, self.ctx)
                    self.app.launch_model_picker.queue.join()
                token = current_token(self.ctx.state_dir, token)
            choose('bridge_launch_model', fields['model'])
            record = self.launch_form(token)
            if fields.get('effort'):
                choose('bridge_launch_budget', str(record['config_revision']) + ':' + fields['effort'])
            for kind, name in (('bridge_launch_workspace', 'workspace'), ('bridge_launch_mode', 'run_mode')):
                if fields.get(name):
                    choose(kind, fields[name])
            event = {**event, 'action_name': SUBMIT_PREFIX + token, 'form_value': {'path': fields.get('path', '')}}
        return event


class LaunchCardCase(UnifiedCase):
    """A launch card published through the real publisher, with its model picker."""

    def prepare_card(self):
        from covey.app.launch import SUBMIT_PREFIX, publish_launch_cards

        self.receive('task', 'pending')
        self.child, self.target = self.route('task')
        with FakeCardKit({'card_id': 'cid'}, sent=('mid', False)) as lark:
            publish_launch_cards(self.child.topics, self.child.voice, self.target, 'chat', 'task')
        self.cards = [call.created for call in lark.calls if call.method == 'POST']
        self.card = self.cards[0]
        self.path = next((self.ctx.state_dir / 'launch-forms').glob('*.json'))
        self.picker = self.app.launch_model_picker
        self.picker.started = True
        self.callback = {'operator_id': 'owner', 'chat_id': 'chat', 'message_id': 'mid',
                         'action_value': {'kind': 'bridge_launch_model', 'token': self.path.stem}, 'option': 'maso:maso-model'}
        self.submit = {'operator_id': 'owner', 'chat_id': 'chat', 'message_id': 'mid', 'action_name': SUBMIT_PREFIX + self.path.stem,
                       'form_value': fill(self.card)}

    def record(self, token=None):
        return self.launch_form(token or self.path.stem)

    def choose(self, kind, option, token=None):
        """A choice on the card the topic shows now; it redraws the card (model_picker.process) on a new
        token, returned."""
        token = current_token(self.ctx.state_dir, token or self.path.stem)
        self.picker.dispatch(self.ctx, {**self.callback, 'event_id': kind + option,
                                        'action_value': {'kind': kind, 'token': token}, 'option': option})
        with FakeCardKit():
            while not self.picker.queue.empty():
                handler, *args = self.picker.queue.get_nowait()
                handler(*args)
        return current_token(self.ctx.state_dir, token)

    def change_model(self):
        token = current_token(self.ctx.state_dir, self.path.stem)
        self.picker.dispatch(self.ctx, {**self.callback, 'action_value': {**self.callback['action_value'], 'token': token}})
        _, ctx, path, revision = self.picker.queue.get_nowait()
        with FakeCardKit() as lark:
            self.picker.process(ctx, path, revision)
        return lark, current_token(self.ctx.state_dir, self.path.stem)


class PickerCase(UnifiedCase):
    """A remembered launch card with its directory picker open."""

    def prepare(self, *, new_topic=False):
        from covey.app.launch import build_launch_card, card_token, remember_launch_card
        from covey.cards.launch_config import PICKER_PREFIX

        self.child, self.target = self.route('source')
        self.form = build_launch_card(self.child.topics, self.target, new_topic=new_topic, draft={'model': 'maso:maso-model', 'effort': 'high'})
        remember_launch_card(self.target, self.form, 'existing-card', 'card-message', sequence=4)
        self.token = card_token(self.form)
        self.record_path = self.ctx.state_dir / 'launch-forms' / (self.token + '.json')
        self.open_event = {'operator_id': 'owner', 'chat_id': 'chat', 'message_id': 'card-message',
                           'action_name': PICKER_PREFIX + self.token,
                           'form_value': {'model': 'maso:maso-model', 'effort': '1:high', 'workspace': self.work.name, 'path': 'draft path',
                                          'run_mode': 'direct'}}
        self.picker = self.app.directory_picker
        self.api = mock.Mock(return_value={'code': 0})
        FakeCardKit(side_effect=route_launch_receipts(self.api, mock.Mock(return_value={'ok': True}))).start(self)
        self.app.runtime.log_sink = mock.Mock()
        return self.picker

    def record(self):
        return json.loads(self.record_path.read_text())

    def current_card(self):
        return json.loads(self.api.call_args.args[3]['card']['data'])

    def action(self, verb, **values):
        return {**self.open_event, 'action_name': '', 'action_value': {
            'kind': 'bridge_directory', 'token': self.token, 'revision': self.record()['picker']['revision'],
            'verb': verb, **values}}

    def click(self, verb, **values):
        self.picker.process(self.ctx, self.action(verb, **values))
