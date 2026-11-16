"""Topics: which Harness app a topic's messages, clicks and jobs go to.

A topic is bound to one Harness (``harness_id`` and ``binding_revision`` in its
topic.json). A new topic is bound to the default Harness until its launch card
selects another; once it has run, its binding changes only by a model switch.
A topic bound to a Harness this release does not have is refused, never rerouted; so is a frozen front-desk
topic (core/topics.py retired_topic): its messages and clicks get its notice (``refuse_retired``).
"""
from __future__ import annotations

import dataclasses
import re

from covey.core.jobs import topic_job_records, upsert_job_record
from covey.core.butler import traits
from covey.core.roster import NAMES, RETIRED
from covey.core.state import write_json
from covey.core.topics import load_topic, read_topic, retired_topic
from covey.core.locks import LOCK, topic_lock
from covey.core.workspaces import topic_context

# A topic recorded without a Harness routes here, the Bridge's own arguments configure it
# and the Bridge's own notices use its voice (their idempotency keys predate the other Harnesses).
DEFAULT = NAMES[0]


def retired_notice(name):
    if name in NAMES:
        return (f'本机器人未启用 {name}，本话题不能继续。'
                '会话仍保留；请启用原后端后续接，或在主聊天新建话题。')
    return (f'本话题使用的 {RETIRED.get(name, "Harness")} 已下架，不能继续。'
            '请在主聊天发送新消息，选择其他模型开始新话题。')


# compat(d3aaeb6): a topic bound to a Harness since removed (core/roster.py RETIRED) gets only its notice;
# retire when no topic.json names one (compat_report: retired-harness)
class RetiredHarness(ValueError):
    """The topic is bound to a Harness this release does not have; nothing may run there."""
    def __init__(self, name, topic):
        super().__init__(retired_notice(name))
        self.name, self.topic = name, topic


def topic_target(app, chat_id, topic):
    """``app``'s context for the topic whose topic.json is ``topic``, as a message in its thread of ``chat_id``."""
    return app.submission.event_context(app.harness.context, {"chat_id": chat_id, "message_id": topic["root_id"]})


def route_locked(ctx):
    data = read_topic(ctx)
    return bool(data.get('harness_locked') or data.get('workspace_locked')
                or any(ctx.history_path.parent.glob('*_session_id'))
                or any(r.get('status') not in {'waiting_workspace', 'cancelled'}
                       for r in topic_job_records(ctx.state_dir, ctx.topic_id)))


class Topics:
    """The Bridge's Harnesses and their apps by name, and the routes of its topics.

    - ``harnesses`` / ``apps``: by Harness name. The Bridge builds each app with this
      object and then enters it in ``apps`` (filled once, before any event).
    - ``runtime`` / ``messenger``: the Bridge's; ``voice``: its own notices' (DEFAULT's).
    """

    def __init__(self, harnesses, *, runtime, messenger) -> None:
        self.harnesses = harnesses
        self.apps = {}
        self.runtime = runtime
        self.messenger = messenger
        self.voice = harnesses[DEFAULT].voice
        self.default = DEFAULT

    def refuse_retired(self, ctx, event, topic_id) -> bool:
        """Whether ``topic_id`` is a frozen front-desk topic (core/topics.py retired_topic); then ``event`` (a message
        or a click in it) got its notice (app/task_controls.py retired_action) and nothing else happens: no history,
        no job, no card write."""
        if not retired_topic(ctx.state_dir, topic_id):
            return False
        app = self.apps.get(self.default) or next(iter(self.apps.values()), None)
        self.runtime.log(f"message refused topic={str(topic_id)[:12]} "
                         f"message={str(event.get('message_id') or '')[-10:]} outcome=retired_topic")
        if app is not None:
            app.controls.retired_action(dataclasses.replace(ctx, topic_id=topic_id), event)
        return True

    def route(self, ctx, event):
        legacy = {'legacy_harness_id': DEFAULT} if self.default != DEFAULT else {}
        topic = topic_context(ctx, event, self.harnesses[self.default].store.sessions_dir,
                              **legacy)
        name = topic.options.get('harness_id', self.default)
        if name not in self.apps:
            raise RetiredHarness(name, topic)
        app = self.apps[name]
        return app, app.submission.event_context(app.harness.context, event)

    def target(self, state_dir, topic_id, chat_id):
        """(app, context, topic.json) of the topic a card names, or None when the id is not a topic's, its
        topic.json cannot be read, its Harness has no app, the topic is another chat's, a frozen front-desk one
        (core/topics.py retired_topic) or it is the butler's (app/butler.py: no root message to address it
        by)."""
        if not isinstance(topic_id, str) or not re.fullmatch("[0-9a-f]{16,128}", topic_id):
            return None
        try:
            topic = load_topic(state_dir, topic_id)
        except (OSError, ValueError):
            return None
        app = self.apps.get(topic.get("harness_id"))
        if (app is None or topic.get("chat_id") != chat_id or not traits(topic_id, chat_id).card_target
                or topic.get("retired")):
            return None
        return app, topic_target(app, chat_id, topic), topic

    def route_or_refuse(self, ctx, event):
        """route(), or None once a message or click in a removed Harness's topic, or a frozen front-desk topic, got
        its notice: neither history nor a job is written, and that topic's session can never run again."""
        try:
            routed = self.route(ctx, event)
            return None if self.refuse_retired(ctx, event, routed[1].topic_id) else routed
        except RetiredHarness as exc:
            self.runtime.log(f"message refused harness={exc.name} message={str(event.get('message_id') or '')[-10:]} "
                     "outcome=retired_harness")
            # Keyed on the click or message: a redelivered event gets no second notice.
            self.messenger.reply(exc.topic, str(event.get('chat_id') or ''), str(exc),
                                 self.voice.key('retired-harness', str(event.get('event_id') or event.get('message_id') or '')), voice=self.voice)
            return None

    def select_harness(self, ctx, chat_id, name):
        """Bind an unstarted topic; existing sessions cannot change Harness."""
        if name not in self.apps:
            raise ValueError('可选 Harness：' + ', '.join(self.apps))
        app = self.apps[name]
        with topic_lock(ctx), LOCK:
            if route_locked(ctx):
                raise ValueError('本话题已开始，配置保持不变；新任务请在主聊天发送消息')
            path = ctx.history_path.parent / 'topic.json'
            data = read_topic(ctx)
            if data.get('harness_id') != name:
                data.update(harness_id=name, binding_revision=int(data.get('binding_revision', 1)) + 1)
                write_json(path, data, ensure_ascii=False)
                # These jobs have never been submitted to a Harness. Rebuild their model
                # selection; prompt_text is the expanded input (forwards, attachments,
                # quotes, the group sender), not Harness-specific, so it stays.
                selected = app.models.configured_context(app.harness.context)
                for record in topic_job_records(ctx.state_dir, ctx.topic_id, status='waiting_workspace'):
                    upsert_job_record(ctx.state_dir, record['key'], backend=name, harness_id=name,
                                      binding_revision=data['binding_revision'], model=selected.model,
                                      effort=selected.effort, model_settings_snapshot=True)
            target = topic_target(app, chat_id, data)
        return target
