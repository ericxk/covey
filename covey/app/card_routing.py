"""Routing what Feishu sends the Bridge: a message (handle_event: pairing, access, the duplicate check, then the
enabled extensions', main-thread and topic routes), a bot menu click (handle_menu_event) and a card click (handle_card_action_event:
access, the enabled extensions' claims, the EARLY and PANELS controller tables, the topic and job routes, then the job's task runner or its Harness's
app). CardRouting is a part of app/unified.py's UnifiedBridgeApp, whose controllers and apps it reads."""
from __future__ import annotations

import dataclasses
import hashlib
import hmac
from operator import attrgetter
import time
from typing import Any

from covey.app import runner_ipc
from covey.app.binding import JOB_STARTING, STALE_CARD, stale_job_card
from covey.app.card_actions import action_value, card_object, click_kind, moved_card_text, ACTIONS
from covey.app.launch import SUBMIT_PREFIX
from covey.core import timing
from covey.core.harness import BridgeContext
from covey.core.locks import LOCK
from covey.core.state import claim_once, read_json, write_durable_json
from covey.core.store import directory_store, one_scan
from covey.core.topics import has_topic_message, message_topic, read_topic
from covey.feishu.message_parts import collect_message_parts

# One empty marker per owner message (and bot menu click) taken in; maintenance drops those a week old.
INBOUND_DIR = 'inbound-messages'


class CardRouting:
    """UnifiedBridgeApp's routes for messages, menu clicks and card clicks."""

    @staticmethod
    def card_lane_key(event: dict[str, Any], ctx: BridgeContext) -> str:
        """The topic a click belongs to; a card outside any topic is its own lane."""
        topic = action_value(event).get("topic_id")
        chat, message = str(event.get("chat_id") or ""), str(event.get("message_id") or "")
        if not isinstance(topic, str) or not topic:
            topic = message_topic(ctx.state_dir, chat, message)
        return "topic:" + topic if topic else "card:" + chat + ":" + message

    def handle_event(self, event, ctx):
        # One timing line per message (core/timing.py): from the consumer reading it to its card.
        flow = timing.begin('message', started=timing.take_arrival(),
                            message=str(event.get('message_id') or event.get('id') or '')[-10:], chat=event.get('chat_type'))
        if flow is not None:
            # Feishu's side: from the message being sent, and from the event being made, to us.
            flow.note(feishu_ms=timing.delay_ms(event.get('create_time'), flow.wall),
                      event_ms=timing.delay_ms(event.get('timestamp'), flow.wall))
        try:
            with one_scan():  # One message reads its job and question records once (core/store.py).
                return self._handle_event(event, ctx)
        finally:
            timing.end(flow)

    def _handle_event(self, event, ctx):
        access = self.runtime.access
        if not self.runtime.owner_open_id and self.pairing_file:
            if event.get('chat_type') == 'group':
                access.ignore(event, 'pairing_in_group')
            else:
                self.pair_owner(event, ctx)
            return
        admitted = access.admit(event)  # Who is served is decided there (app/access.py).
        timing.mark('access')
        if admitted is None:
            timing.outcome('ignored')
            return
        # Feishu delivers at least once: the same message again, even after a restart, is not a new task.
        message_id = str(event.get('message_id') or event.get('id') or '')
        claimed = not message_id or claim_once(ctx.state_dir / INBOUND_DIR, hashlib.sha256(message_id.encode()).hexdigest())
        timing.mark('dedup')
        if not claimed:
            timing.outcome('duplicate')
            self.runtime.log(f'duplicate message id={message_id} skipped')
            return
        if admitted.owner and (not admitted.group or access.agent_chat(str(event.get('chat_id') or ''), True)) \
                and not str(event.get('thread_id') or ''):
            self.menu.owner_said(str(event.get('chat_id') or ''))  # The menu's cards there are no longer the newest.
        if admitted.owner and admitted.group:
            # A group sends no read receipts: the owner's message there reads its board (app/group_boards.py).
            self.group_boards.owner_active(ctx, str(event.get('chat_id') or ''))
        if admitted.group:
            event = access.strip(event)  # The prompt sees the text without the bot's @.
            if not admitted.owner:
                event = access.member_text(event)
            else:
                # The owner's @ activates the group (app/access.py); there are no text commands (the bot menu).
                access.owner_message(ctx, str(event.get('chat_id') or ''), self.runtime.owner_open_id)
        if admitted.group and not str(event.get('content') or '').strip():
            timing.outcome('ignored')
            access.ignore(event, 'empty')  # Only the bot's @ (an owner's has activated the group): nothing to run.
            return
        if any(feature.handle_event(ctx, event, admitted.owner) for feature in self.extension_features
               if hasattr(feature, 'handle_event')):
            return
        if self.main_thread.handle_event(ctx, event, admitted.owner):
            return
        # Every other text is a task, a supplement or a group message.
        routed = self.topics.route_or_refuse(ctx, event)
        timing.mark('route')
        if routed is None:
            timing.outcome('refused')
            return
        app, target = routed
        return app.handle_event(event, target)

    def handle_menu_event(self, event, ctx):
        """A click on the bot menu (app/bot_menu.py): one ``timing`` line and one log line."""
        flow = timing.begin('menu', started=timing.take_arrival(), key=str(event.get('event_key') or '')[:40])
        outcome = 'error'
        try:
            outcome = self.menu.handle(event, ctx)
        finally:
            timing.outcome(outcome)
            timing.end(flow)
            self.runtime.log(f"bot menu key={str(event.get('event_key') or '')[:40]} "
                             f"event={str(event.get('event_id') or '')[-10:]} outcome={outcome}")

    def pair_owner(self, event, ctx):
        parts, _, _ = collect_message_parts(str(event.get('content') or ''))
        message = '\n'.join(parts).strip()
        sender = str(event.get('sender_id') or '')
        if not sender or not message.startswith('/pair '):
            return
        digest = hashlib.sha256(message[len('/pair '):].strip().encode()).hexdigest()
        with LOCK:
            if self.runtime.owner_open_id:
                return
            pair = read_json(self.pairing_file)
            identity_path = ctx.state_dir / 'bot-identity.json'
            identity = read_json(identity_path)
            if (pair.get('app_id') != identity.get('app_id') or pair.get('expires_at', 0) <= time.time()
                    or not hmac.compare_digest(pair.get('sha256', ''), digest)):
                return
            identity['owner'] = sender
            write_durable_json(identity_path, identity)
            self.runtime.owner_open_id = sender
            self.pairing_file.unlink()
        self.messenger.send_message(ctx.profile, event['chat_id'],
                                    '已绑定你的身份。发送任务后，在该消息下选择模型、推理强度和工作区。',
                                    self.voice.key('paired', event['message_id']), voice=self.voice)

    def handle_card_action_event(self, event, ctx):
        started = time.monotonic()
        try:
            with one_scan():  # One click reads its records once; its follow-ups (after_ack) run after.
                outcome = self._handle_card_action_event(event, ctx)
        except Exception:
            outcome = 'error'
            raise
        finally:
            timing.note(outcome=outcome)
            # One line per click: tells "the click never arrived" from "it arrived and was refused".
            self.runtime.log(f"card action kind={click_kind(event)} message={str(event.get('message_id') or '')[-10:]} "
                     f"event={str(event.get('event_id') or '')[-10:]} outcome={outcome} "
                     f"elapsed_ms={(time.monotonic() - started) * 1000:.0f}")

    def _handle_card_action_event(self, event, ctx):
        # An option line stands for a select's option and carries it in its value (cards/launch_config.py
        # setting_row): its click reads as that select's callback.
        if event.get('option') is None and isinstance(action_value(event).get('option'), str):
            event['option'] = action_value(event)['option']
        operator = str(event.get('operator_id') or '')
        allowed = self.runtime.access.allows(operator, str(event.get('chat_id') or ''))
        timing.mark('access')
        if not allowed:
            return 'not_owner'
        # The owner's controls: the controller refuses anyone else on the card (owner_only).
        owner = self.runtime.access.is_owner(operator)
        # A card of a frozen front-desk topic (core/topics.py retired_topic): every button on it, 换配置 and 回退
        # too, gets the one notice and does nothing (rules 8 and 10).
        topic = message_topic(ctx.state_dir, str(event.get('chat_id') or ''), str(event.get('message_id') or ''))
        if topic and self.topics.refuse_retired(ctx, event, topic):
            return 'retired_topic'
        raw = card_object(event, 'action_value')
        for feature in self.extension_features:  # An enabled extension's cards (core/extensions.py).
            if hasattr(feature, 'claim') and (outcome := feature.claim(ctx, event, raw or {}, owner)) is not None:
                return outcome
        if (outcome := self.first_claim(self.EARLY, ctx, event, raw, owner)) is not None:
            return outcome
        if raw is None:
            return 'bad_value'
        if not str(event.get('action_name') or '').startswith(SUBMIT_PREFIX) and raw.get('kind') not in ACTIONS:
            return self.moved_card(ctx, event) or 'retired'  # Closed allowlist also retires already-sent settings/menu cards.
        if (outcome := self.first_claim(self.PANELS, ctx, event, raw, owner)) is not None:
            return outcome
        event = dict(event)
        item = None  # The card's message as Feishu returned it, when the topic aliases do not know it.
        if not has_topic_message(ctx.state_dir, event.get('chat_id', ''), event.get('message_id', '')):
            from covey.feishu.message_read import apply_message_metadata, message_item, fetch_message_detail
            detail = fetch_message_detail(event.get('message_id', ''), ctx.profile)
            item = message_item(detail, event.get('message_id', '')) if detail else None
            if not item:
                return 'unknown_message'
            apply_message_metadata(event, item)
        routed = self.topics.route_or_refuse(ctx, event)
        timing.mark('route')
        if routed is None:
            return 'retired_harness'  # Every old control there: stop, retry, fork, answer, supplement, files.
        app, target = routed
        if raw.get('topic_id') and raw['topic_id'] != target.topic_id:
            return 'other_topic'
        routed = self.job_route(app, target, event, raw)
        timing.mark('job_route')
        if routed is None:
            return 'stale_job'
        app, target = routed
        if 'harness_id' in raw or 'binding_revision' in raw:
            if (raw.get('harness_id') != target.options['harness_id']
                    or raw.get('binding_revision') != target.options['binding_revision']):
                app.messenger.reply(target, event['chat_id'], '此卡片已过期，请使用本话题最新的任务卡。',
                                    app.key('stale-route', event.get('event_id') or event['message_id']), voice=app.voice)
                return 'stale_route'
        if runner_ipc.forward_card_action(self.runtime, target, event, raw):
            timing.mark('forward')
            return 'forwarded'  # The job's task runner owns its card.
        app.handle_card_action_event(event, target, item=item)
        return 'handled'

    def moved_card(self, ctx, event):
        """A click on a card of an extension that is not enabled (card_actions.py MOVED_CARDS): the one-line notice
        under the card, nothing else happens; the outcome, or None when the card is not such a one."""
        text = moved_card_text(event)
        if not text:
            return None
        message_id = str(event.get('message_id') or '')
        self.messenger.reply(dataclasses.replace(ctx, reply_to_message_id=message_id), str(event.get('chat_id') or ''), text,
                             self.voice.key('extension-missing', str(event.get('event_id') or message_id)), voice=self.voice)
        return 'extension_missing'

    def first_claim(self, table, ctx, event, raw, owner):
        """The outcome of the first controller in ``table`` (EARLY, PANELS) that claims the click; None
        when none does."""
        for name in table:
            if (outcome := attrgetter(name)(self).claim(ctx, event, raw, owner)) is not None:
                return outcome
        return None

    def rotate_dashboard(self, ctx, event, owner, raw=None):
        """换口令 on a 脱敏 link's card: a new token and its link, once a click; the new card replaces the
        menu's last one like a menu click's. On the 设置 card's 脱敏 tab (``raw`` carries its token) the same card
        shows the new link (SettingsHub.rotate)."""
        event_id = str(event.get('event_id') or '')
        if not owner:
            return 'owner_only'
        if not event_id or not claim_once(ctx.state_dir / INBOUND_DIR,
                                          hashlib.sha256(('rotate:' + event_id).encode()).hexdigest()):
            return 'duplicate'
        if self.settings_hub.rotate(ctx, event, raw or {}):
            return 'dashboard_rotate'
        chat_id = str(event.get('chat_id') or '')
        self.dashboard.send_link(ctx, chat_id, event_id, rotate=True,
                                 shown=self.menu.shown(ctx, 'redaction_dashboard', chat_id, event_id))
        return 'dashboard_rotate'

    def job_route(self, app, target, event, raw):
        """A task card belongs to its job's Harness, which a model switch may have replaced.

        Viewing its process or artifacts goes to that Harness; starting a job from it
        (retry) is refused. None when the click was refused."""
        job_id = raw.get('job_id')
        if not job_id or not target.topic_id:
            return app, target
        try:
            record = next((r for _, r in directory_store(target.state_dir / 'jobs').query(
                topic_id=target.topic_id, chat_id=event.get('chat_id'), job_id=int(job_id))), None)
        except (TypeError, ValueError):
            return app, target
        if record is None:
            return app, target
        topic = read_topic(target)
        if raw.get('kind') in JOB_STARTING and stale_job_card(topic, record):
            app.messenger.reply(target, event['chat_id'], STALE_CARD,
                                app.key('stale-job', event.get('event_id') or event['message_id']), voice=app.voice)
            return None
        owner = self.apps.get(record.get('backend'))
        if owner is None or owner is app:
            return app, target
        return owner, owner.submission.event_context(owner.harness.context, event)

