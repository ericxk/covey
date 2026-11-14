"""The 重新发送 card: a 代答 reply that never went out, offered to the owner to send again (app/proxy.py).

When every try of a send ended before Enter was pressed (certainly not sent), the worker shows the card
in the contact's topic, under its messages (cards/proxy_resend.py), instead of a line of text; a batch
held without it (the card did not go out, or an older release said it in text) gets it on the next poll. 重新发送
sends the batch's saved reply again as it is, without running the model. The click only decides: the
owner (app/access.py, through the caller), the batch still held with the reply the card shows, 代答
on, the contact on the whitelist and not paused, under the hourly limit. Under Proxy.lock it marks the
batch (``resend_requested``, its tries back to 0) and the card is redrawn as sending, its button off.
The worker's next poll sends it (Proxy.web_lock: one browser), also past STALE_AFTER since the owner
asked, and the card shows what came of it: sent (read back), not sent again (the button back), or
maybe sent (uncertain: no button; it is never sent again). A batch that ends another way (newer
messages, its contact removed, nothing to say back) has its card redrawn so, without the button
(``close``), and a click on one says so on the card; a refusal is written on the card.

按建议回复 (repo review U5) is the same click on the same card for a turn that would not answer ([不回复]) and
suggested a reply (``declined``, drawn by the worker instead of a ⏸ line): the batch, held, keeps the
suggestion as its reply, and the click marks it to be sent the same way, as the owner's own choice; from
then on the card is the 重新发送 card.

The card's own record is <state>/proxy/resend/<batch id>.json: card_id, message_id, the sequence of
its last write and the view drawn. The batch keeps ``resend_card`` {card_id, message_id}; the
sequence stays out of it, since the worker writes a batch whole from a copy it read earlier, which
would take a sequence back. Every write is the whole card under the card's own lock, its sequence from
``reserve`` and its key made from it (``replace_form_card``). Only the Bridge writes it: the click is
handled before topic routing, never by a task runner.
"""
from __future__ import annotations

import re
import time

from covey.app.form_cards import replace_form_card
from covey.cards.proxy_resend import RESEND, SUGGEST, reply_digest, resend_card
from covey.core import proxy as records
from covey.core.locks import resource_lock
from covey.core.state import read_object, write_json
from covey.feishu.cardkit import extract_card_id

OWNER_ONLY = '⚠️ 仅机器人主人可以重新发送代答。'
SUGGEST_OWNER_ONLY = '⚠️ 仅机器人主人可以按建议回复。'
CHANGED = '⚠️ 卡片刚更新过，要发的内容已经换了，这次点击没有执行。请看卡上现在的内容再点。'
SETTLED = {'superseded': 'void', 'sent': 'sent', 'uncertain': 'uncertain'}


def save(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, record, ensure_ascii=False)


class ProxyResend:
    def __init__(self, proxy):
        self.proxy = proxy

    @staticmethod
    def path(ctx, batch_id):
        return records.proxy_dir(ctx.state_dir) / 'resend' / f'{batch_id}.json'

    @staticmethod
    def matches(value):
        return value.get('kind') in (RESEND, SUGGEST)

    def claim(self, ctx, event, raw, owner):
        """The click's outcome when it is a 重新发送 card's, else None (app/unified.py PANELS): the
        Bridge's own card in a 代答 topic."""
        return self.click(ctx, event, raw, owner) if self.matches(raw) else None

    @staticmethod
    def record(path):
        return read_object(path)

    @staticmethod
    def names(ctx, batch):
        """(whom the reply is for, the chat it goes to): the sender of the batch's last message, else its
        contact; the contact's chat title (a group's name), else that person."""
        contact = next((c for c in records.read_contacts(ctx.state_dir) if c['key'] == batch['contact']), None)
        who = batch['messages'][-1].get('sender_name') or (contact or {}).get('name') or '对方'
        return who, (contact or {}).get('name') or who

    def card(self, ctx, batch, view):
        return resend_card(view, batch, *self.names(ctx, batch))

    def publish(self, ctx, path, record, batch, view):
        """The whole card drawn for ``view``, saved with it; False when CardKit did not confirm."""
        card = self.card(ctx, batch, view)
        return replace_form_card(ctx, path, record, {**record, 'view': view}, card, self.proxy.messenger, 'proxy-resend')

    def show(self, ctx, settings, batch, state, notice=''):
        """The batch's card showing ``state``: redrawn when it has one, else sent in its topic under its
        messages, once. The card is created first and kept, so a send tried again carries the same card
        under the same key. The batch (with ``resend_card`` once the card is out); None when the card
        could not be shown. It calls Feishu: never under Proxy.lock."""
        proxy = self.proxy
        path = self.path(ctx, batch['id'])
        view = {'state': state, 'notice': notice}
        with resource_lock('proxy-resend', path):
            record = self.record(path)
            if record.get('message_id'):
                return batch if self.publish(ctx, path, record, batch, view) else None
            if not record.get('card_id'):
                card = self.card(ctx, batch, view)
                card_id = extract_card_id(proxy.messenger.create_card(ctx.profile, card) or {})
                if not card_id:
                    return None
                record = {'batch': batch['id'], 'chat_id': settings['group_chat_id'], 'card_id': card_id,
                          'sequence': 0, 'view': view}
                save(path, record)
            elif record.get('view') != view:
                # Created and never sent out: brought up to date before it goes.
                if not self.publish(ctx, path, record, batch, view):
                    return None
                record = self.record(path)
            message_id, _ = proxy.messenger.send_card(ctx.profile, record['chat_id'], record['card_id'],
                                                      proxy.voice.key('proxy-resend', batch['id']), voice=proxy.voice,
                                                      reply_to_message_id=batch.get('mirror_id') or '')
            if not message_id:
                return None
            save(path, {**record, 'message_id': message_id})
        with proxy.lock:
            batch = {**(records.read_batch(ctx.state_dir, batch['id']) or batch),
                     'resend_card': {'card_id': record['card_id'], 'message_id': message_id}}
            records.write_batch(ctx.state_dir, batch)
        proxy.runtime.log(f"proxy resend card sent batch={batch['id'][:8]} message={message_id[-10:]}")
        return batch

    def close(self, ctx, batch):
        """``batch`` ended some other way than by a send (newer messages, its contact removed, nothing to say
        back): its card, when one is out, redrawn to say so, without its button. A card left saying it was
        being sent again, its button off, once stayed so for good (repo review D7). Never under Proxy.lock.
        None (core/proxy.py move_batch: it moved on meanwhile) is left to what moved it."""
        if batch is None or not batch.get('resend_card'):
            return
        path = self.path(ctx, batch['id'])
        view = {'state': SETTLED.get(batch['status'], 'dropped'), 'notice': ''}
        with resource_lock('proxy-resend', path):
            record = self.record(path)
            if record.get('message_id') and record.get('view') != view:
                self.publish(ctx, path, record, batch, view)

    def click(self, ctx, event, value, owner):
        """A click on 重新发送; the outcome its ``card action`` line records."""
        batch_id = value.get('batch')
        if not isinstance(batch_id, str) or not re.fullmatch('[0-9a-f]{32}', batch_id):
            return 'proxy_unknown'
        proxy = self.proxy
        path = self.path(ctx, batch_id)
        with resource_lock('proxy-resend', path):
            record = self.record(path)
            batch = records.read_batch(ctx.state_dir, batch_id)
            if batch is None or not record.get('card_id') or record.get('message_id') != event.get('message_id'):
                return 'proxy_unknown'
            shown = record.get('view') or {}
            if not owner:
                self.publish(ctx, path, record, batch,
                             {**shown, 'notice': SUGGEST_OWNER_ONLY if value.get('kind') == SUGGEST else OWNER_ONLY})
                return 'owner_only'
            with proxy.lock:
                batch = records.read_batch(ctx.state_dir, batch_id) or batch
                view, outcome, batch = self.decide(ctx, batch, value, shown, str(event.get('operator_id') or ''))
            self.publish(ctx, path, record, batch, view)
        if outcome == 'proxy_resend':
            proxy.runtime.log(f'proxy resend requested batch={batch_id[:8]}'
                              + (' (the suggested reply)' if value.get('kind') == SUGGEST else ''))
        return outcome

    def decide(self, ctx, batch, value, shown, operator):
        """(view, outcome, batch to draw) of the owner's click: the batch marked to be sent again, or why
        not. Under Proxy.lock: records only, no Feishu."""
        status = batch['status']
        first = shown.get('state') in ('declined', 'sending')  # 按建议回复: nothing was sent for this batch yet.
        sending, verb, button = ('sending', '发出', '按建议回复') if first else ('resending', '重新发送', '重新发送')
        if status in SETTLED:
            return {'state': SETTLED[status], 'notice': ''}, 'proxy_stale', batch
        if status == 'sending' or status == 'held' and batch.get('resend_requested'):
            return {'state': sending, 'notice': ''}, 'proxy_resend_pending', batch
        if status != 'held' or not batch.get('reply'):
            return {'state': 'dropped', 'notice': ''}, 'proxy_stale', batch
        state = shown.get('state') if shown.get('state') in ('failed', 'again', 'declined') else 'failed'
        if value.get('reply') != reply_digest(batch['reply']):
            return {'state': state, 'notice': CHANGED}, 'proxy_stale', batch
        asked = {**batch, 'resend_requested': time.time(), 'resend_by': operator}
        why = self.proxy.stopped(ctx, asked, asked.get('reply_job_id'))
        if why and why[0] == records.REMOVED:
            return {'state': 'dropped', 'notice': ''}, 'proxy_resend_refused', {**batch, 'reason': why[0]}
        if why:
            return ({'state': state, 'notice': f'⚠️ 没有{verb}：{why[0]}。{why[1]}再点“{button}”。'},
                    'proxy_resend_refused', batch)
        if records.recent_sends(records.read_batches(ctx.state_dir, statuses=None), batch['contact'],
                                time.time()) >= records.HOURLY_LIMIT:
            return ({'state': state, 'notice': f'⚠️ 没有{verb}：一小时内已给对方代答 {records.HOURLY_LIMIT} 条，过一会儿再点。'},
                    'proxy_resend_refused', batch)
        asked.update(attempts=0, send_failed=True)
        records.write_batch(ctx.state_dir, asked)
        return {'state': sending, 'notice': ''}, 'proxy_resend', asked
