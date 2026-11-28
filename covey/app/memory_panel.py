"""The 设置 card's 记忆 tab (cards/memory_panel.py), shown when COVEY_MEMORY=1: Feishu keeps the numbers, the light handling of
待审核 and the way to the web page; managing the memory is the 网页记忆中心's (app/web_memory.py).

What it reads (``facts``), at each draw: ``web_memory.overview`` (the rules in use of 全局, the projects and the topics, and of
待审核, counted; the open items of 待审核 the oldest first; those rules) and the page's address (``web_memory.center_url``; ''
without the web page, then the card lists the rules to 收起, the topics' too).

A click (the owner's) acts outside the record's lock, then the whole card is drawn again under it once the click is
acknowledged (``notice`` says what happened). 采纳 and 不要 on an item of 待审核 go through ``web_memory.review_decide`` (as the page
does); 收起 of a rule asks first, then ``memstore.archive``. The kinds of the older tab (the 工作区 row, 升为全局, 常驻) are still
taken, so a card drawn before does not hang: they change nothing and say the option is offline. The same click delivered again
acts once: the view keeps the last ``seen`` event ids.
"""
from __future__ import annotations

import secrets

from covey.app import web_memory
from covey.app.card_lanes import after_ack
from covey.app.menu_panel import FOLDED, MenuPanel, hub_top
from covey.cards.memory_panel import ACCEPT, DELETE, KINDS, OFFLINE, OPEN, REVIEW, RULES, memory_card
from covey.cards.settings_hub import MEMORY
from covey.core import memstore

SEEN = 8
OWNER_ONLY = '⚠️ 仅机器人主人可以改记忆。'
GONE = '这条已经不在了，卡片按现在的记忆重画了。'


def click_token() -> str:
    """The undo token of one change made from the tab (core/memstore.py log_change): its own, so the 记录 page of the web's
    memory center can take that one click back."""
    return f"feishu-{secrets.token_hex(6)}"


def short(text, limit=24) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def facts(state_dir) -> dict:
    """The tab's data (cards/memory_panel.py): ``web_memory.overview`` and the page's address; ``error`` says so when the memory
    cannot be read (the card is still drawn)."""
    try:
        data = dict(web_memory.overview(state_dir))
    except Exception as exc:  # noqa: BLE001 - A broken store is a line on the card, not a card that does not draw.
        data = {"counts": {}, "rules_by": {}, "scopes": [], "review": [], "rules": [],
                "error": f"⚠️ 记忆读不出来（{type(exc).__name__}），稍后再看。"}
    data["url"] = web_memory.center_url()
    return data


class MemoryPanel:
    def __init__(self, runtime, messenger, voice):
        self.runtime, self.messenger, self.voice = runtime, messenger, voice
        self.menu = MenuPanel(self, 'memory-panel', 'memory-cards', 'memory-card', tab=MEMORY)

    def path(self, ctx, token):
        return self.menu.path(ctx, token)

    @staticmethod
    def matches(value):
        return value.get('kind') in KINDS

    def claim(self, ctx, event, raw, owner):
        return self.click(ctx, event, raw, owner) if self.matches(raw) else None

    @staticmethod
    def tab_view(token):
        return {'token': token, 'revision': 1, 'hub': True, 'review_page': 0, 'rules_page': 0}

    @staticmethod
    def tab_fields(ctx):
        return {}

    def tab_card(self, ctx, token, record):
        return self.draw(ctx, token, record['view'])

    def tab_shown(self, ctx, token):
        self.runtime.log(f'memory tab shown token={token[:8]}')

    def refuse(self, ctx, path, event):
        return self.menu.refuse(ctx, path, event, OWNER_ONLY, lambda view, record: self.draw(ctx, path.stem, view))

    def draw(self, ctx, token, view):
        return memory_card(token, view, facts(ctx.state_dir), top=hub_top(MEMORY, token, view))

    def click(self, ctx, event, value, owner):
        entry = self.menu.entry(ctx, event, value, unfolded=True)
        if entry is None:
            return 'memory_unknown'
        token, path, record = entry
        if not owner:
            self.refuse(ctx, path, event)
            return 'owner_only'
        event_id = str(event.get('event_id') or '')
        if event_id and event_id in (record['view'].get('seen') or ()):
            return 'duplicate'
        changes = self.act(ctx, value, record['view'])
        after_ack(lambda: self.redraw(ctx, path, event, changes, event_id))
        return 'memory_tab'

    def redraw(self, ctx, path, event, changes, event_id=''):
        with self.menu.lock(path):
            record = self.menu.clicked(path, event)
            if record is None or record.get(FOLDED):
                return False
            view = {**record['view'], 'notice': '', **changes, 'token': path.stem}
            if event_id:
                view['seen'] = [*(view.get('seen') or ())][-(SEEN - 1):] + [event_id]
            view['revision'] += 1
            confirmed = self.menu.publish(ctx, path, record, view, self.draw(ctx, path.stem, view))
        if not confirmed and view.get('notice'):
            self.menu.unsaved(ctx, record, view['notice'], f"{path.stem}-{view['revision']}")
        return True

    def act(self, ctx, value, view):
        """The view's changes for the click; the store written first when it decides or puts away (no Feishu call)."""
        kind = value.get('kind')
        if kind == OPEN:
            scope, page = value.get('scope'), value.get('page')
            if scope not in (REVIEW, RULES) or isinstance(page, bool) or not isinstance(page, int) or page < 0:
                return {}
            return {f'{scope}_page': page, 'confirm': None}
        key, op = value.get('key'), value.get('op')
        if kind == ACCEPT and isinstance(key, str) and key.startswith('review:') and op == 'accept':
            return self.decide(ctx, key, 'accept', '已采纳')
        if kind == DELETE and isinstance(key, str) and key.startswith('review:') and op == 'reject':
            return self.decide(ctx, key, 'reject', '已选「不要」')
        if kind == DELETE and isinstance(key, str) and op in ('ask', 'cancel', 'confirm'):
            return self.put_away(ctx, key, op, view)
        self.runtime.log(f'memory tab click of an offline option kind={kind}')
        return {'confirm': None, 'notice': OFFLINE}

    def decide(self, ctx, key, action, words):
        title = next((row.get('title') or row.get('text') for row in memstore.review_items(ctx.state_dir) if row.get('key') == key), None)
        try:
            web_memory.review_decide(ctx.state_dir, [key], action, who='owner', token=click_token())
        except ValueError as exc:  # Already decided, or the store says why not.
            return {'notice': f'⚠️ {exc}'}
        self.runtime.log(f'memory review decided from the 记忆 tab action={action}')
        return {'confirm': None, 'notice': f'{words}：{short(title)}' if title else words}

    def put_away(self, ctx, key, op, view):
        """收起 of a rule: ``ask`` marks it for 确认收起, ``cancel`` unmarks, ``confirm`` archives it (only a rule in use: the
        click of an older card for another entry finds it not there)."""
        if op == 'ask':
            return {'confirm': key}
        if op == 'cancel':
            return {'confirm': None}
        if key not in {row.get('key') for row in web_memory.overview(ctx.state_dir).get('rules') or ()}:
            return {'confirm': None, 'notice': GONE}
        try:
            entry = memstore.archive(ctx.state_dir, key, who='owner', token=click_token())
        except ValueError as exc:
            return {'confirm': None, 'notice': f'⚠️ {exc}'}
        self.runtime.log('memory rule put away from the 记忆 tab')
        return {'confirm': None, 'notice': f'已收起：{entry.code} {short(entry.title)}'}
