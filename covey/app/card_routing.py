"""Routing what Feishu sends the Bridge: a message (handle_event: pairing, access, the duplicate check, then the
enabled extensions', butler and topic routes), a bot menu click (handle_menu_event) and a card click (handle_card_action_event:
access, the enabled extensions' claims, the EARLY and PANELS controller tables, the topic and job routes, then the job's task runner or its Harness's
app). CardRouting is a part of app/unified.py's UnifiedBridgeApp, whose controllers and apps it reads."""
from __future__ import annotations

import dataclasses
import hashlib
import hmac
from operator import attrgetter
import time
from typing import Any

from covey.app import butler_tracks, dispatch_cards, relay_stops, runner_ipc, strangers
from covey.app.binding import JOB_STARTING, STALE_CARD, stale_job_card
from covey.app.card_actions import action_value, card_object, click_kind, moved_card_text, ACTIONS
from covey.cards.launch_config import SUBMIT_PREFIX
from covey.core import inbound_ledger, timing
from covey.core.harness import BridgeContext
from covey.core.jobs import JOB_STOP_ACTION_KIND
from covey.core.locks import LOCK
from covey.core.state import claim_once, read_json, write_durable_json
from covey.core.store import directory_store, one_scan
from covey.core.topics import has_topic_message, message_topic, read_topic
from covey.feishu.events import with_parent
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
                # The quoted message as parent_id before any route reads it (lark-cli and the WS intake say reply_to).
                return self._handle_event(with_parent(event), ctx)
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
            strangers.answer(self.runtime, self.messenger, self.voice, ctx, event)  # A person's direct message.
            return
        # Feishu delivers at least once: the same message again, even after a restart, is not a new task.
        message_id = str(event.get('message_id') or event.get('id') or '')
        claimed = not message_id or claim_once(ctx.state_dir / INBOUND_DIR, hashlib.sha256(message_id.encode()).hexdigest())
        timing.mark('dedup')
        if not claimed:
            timing.outcome('duplicate')
            self.runtime.log(f'duplicate message id={message_id} skipped')
            if admitted.owner and inbound_ledger.lost_before(ctx.state_dir, message_id):
                self.tell_lost(ctx, event, message_id)
            return
        if admitted.owner:
            inbound_ledger.received(ctx.state_dir, event)  # Where it goes is noted as it is decided (core/inbound_ledger.py).
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
                # The owner's first @ in a group asks him in his private chat whether to open it to its members
                # (app/access.py owner_message, app/group_manager.py ask); there are no text commands (the bot menu).
                ask = access.owner_message(ctx, str(event.get('chat_id') or ''), self.runtime.owner_open_id)
                if ask is not None:
                    self.group_manager.ask(ctx, ask)
        if admitted.group and not str(event.get('content') or '').strip():
            timing.outcome('ignored')
            access.ignore(event, 'empty')  # Only the bot's @ (an owner's may have asked him about the group).
            return
        if self.menu.handle_slash(
                ctx, event, admitted,
                personal_group=admitted.group and access.agent_chat(str(event.get('chat_id') or ''), True)):
            inbound_ledger.note(ctx.state_dir, message_id, 'command')
            return
        if any(feature.handle_event(ctx, event, admitted.owner) for feature in self.extension_features
               if hasattr(feature, 'handle_event')):
            inbound_ledger.note(ctx.state_dir, message_id, 'extension')
            return
        if self.butler.handle_event(ctx, event, admitted.owner):
            return
        # Every other text is a task, a supplement or a group message.
        routed = self.topics.route_or_refuse(ctx, event)
        timing.mark('route')
        if routed is None:
            timing.outcome('refused')
            inbound_ledger.note(ctx.state_dir, message_id, 'refused', reason='所在话题已冻结或它的 Harness 已下线，回复在话题里')
            return
        app, target = routed
        return app.handle_event(event, target)

    def tell_lost(self, ctx, event, message_id):
        """The owner's message was taken in once and never handled (the Bridge stopped between: its duplicate now comes
        from the event spool's replay): said once in its thread, so he sends it again. No lock held; the reply is keyed."""
        inbound_ledger.note(ctx.state_dir, message_id, 'lost')
        chat = str(event.get('chat_id') or '')
        self.runtime.log(f'message lost id={message_id[-10:]} told')
        self.messenger.reply(dataclasses.replace(ctx, reply_to_message_id=message_id), chat,
                             '这条消息收下后没来得及处理就中断了（Bridge 重启过），没有变成任何一轮，请再发一次。',
                             self.voice.key('lost-message', message_id), voice=self.voice)

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
        if raw.get('kind') in dispatch_cards.KINDS and not self.runtime.in_runner:
            # A 派发卡's 补充 and 停止 act on what the topic is doing when they are clicked, not on the job the card
            # named when it was last drawn; the task runner a forward reaches takes it as it is.
            job_id = dispatch_cards.click_job(self.runtime, target.state_dir, target.topic_id, event.get('chat_id'))
            if not job_id:
                app.messenger.reply(target, event['chat_id'], dispatch_cards.NO_JOB,
                                    app.key('dispatch-idle', event.get('event_id') or event['message_id']), voice=app.voice)
                return 'dispatch_idle'
            raw = {**raw, 'job_id': job_id}
            event['action_value'] = raw
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
        self.count_others(target, event, raw)
        if not self.runtime.in_runner and raw.get('kind') in (dispatch_cards.STOP_KIND, JOB_STOP_ACTION_KIND):
            dispatch_cards.stop_clicked(target.topic_id, raw.get('job_id'))  # Its 派发卡 says 正在停止 at the next tick.
        if runner_ipc.forward_card_action(self.runtime, target, event, raw):
            timing.mark('forward')
            return 'forwarded'  # The job's task runner owns its card.
        app.handle_card_action_event(event, target, item=item)
        return 'handled'

    def moved_card(self, ctx, event):
        """A click on a card of a feature that was taken out (card_actions.py MOVED_CARDS): the one-line notice
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

    def count_others(self, target, event, raw):
        """A stop button on a task of the butler's, or on a 派发卡: how many other tracks of the butler are still going
        goes into the click as ``others`` (app/butler_tracks.py ``stop_others``, the stop word's count), for the Bridge's
        reply (app/task_controls.py) or, forwarded, the task runner's, which does not see the other tasks. Any other click
        is left as it is."""
        dispatched = raw.get('kind') == dispatch_cards.STOP_KIND
        if self.runtime.in_runner or not (dispatched or raw.get('kind') == JOB_STOP_ACTION_KIND):
            return
        others = butler_tracks.stop_others(self.runtime, target.state_dir, str(event.get('chat_id') or ''), target.topic_id,
                                           raw.get('job_id'), dispatched=dispatched)
        if others > 0:
            event['action_value'] = {**raw, 'others': str(others)}

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
            # A stopped relay's 检查并继续 at the top level says so on its own card, redrawn (app/relay_stops.py); a task card's
            # click gets the reply in the topic's thread.
            if not (raw.get(relay_stops.RELAY) and record.get('key')
                    and relay_stops.settle(target.state_dir, app.messenger, target.profile, record['key'], record)):
                app.messenger.reply(target, event['chat_id'], STALE_CARD,
                                    app.key('stale-job', event.get('event_id') or event['message_id']), voice=app.voice)
            return None
        owner = self.apps.get(record.get('backend'))
        if owner is None or owner is app:
            return app, target
        return owner, owner.submission.event_context(owner.harness.context, event)

