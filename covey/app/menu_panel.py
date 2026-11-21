"""A card the bot menu sends to the owner's private chat (代答 app/proxy_panel.py, 群聊 app/group_manager.py,
工作区 app/workspace_manager.py): no topic, no job, one card a menu click.

Its record is <state>/<folder>/<token>.json, changed under the resource lock ``lock_name``; it holds the
card's view in ``view_field``. ``via`` is what sends for the card: its ``messenger`` and ``voice``, looked up
at each call.

The bot menu's 设置 (app/settings_hub.py) is one card showing one of its panels (the butler's 配置, the 代答 or the
群聊 panel) under a tab row, held by one record of its own: <state>/HUB_FOLDER/<token>.json, with the tab it shows
(``tab``) and that tab's view (``hub`` in it: ``hub_top`` draws the row). A panel made with its ``tab`` (or tabs: the
butler's pages) finds that record by the token its buttons carry (``path``), changes it under the one lock
HUB_LOCK (``lock``) and acts on a click only while the card shows its tab (``clicked``, ``shows``); its writes take
their sequence from that one record (rule 3).

A second click on the menu item folds its card in place (``toggle``, app/bot_menu.py decides when): the record
keeps FOLDED and the card shows its title bar and one grey line; the next click draws it whole again from a fresh
first view, as a new card would be. Every write takes the record's next sequence; a redraw of the whole card
(``publish``) leaves it unfolded. A record without FOLDED is unfolded.
"""
from __future__ import annotations

import re

from covey.app.form_cards import click_token, publish_form, settle
from covey.cards.settings_hub import LABELS, MEMORY, NODES, PROXY, RETIRED, tab_row
from covey.core import memory, nodes
from covey.core.locks import resource_lock
from covey.core.state import read_json

OPEN = 'open'
# A panel's own record of a 设置 card from before it had one record (gen 286): the card went on to another tab,
# another record holds it now (``moved_to``); its clicks find nothing.
MOVED = 'moved'
HUB_FOLDER = 'settings-cards'  # The 设置 cards' records (app/settings_hub.py).
HUB_LOCK = 'settings-card'
FOLDED = 'folded'  # True while the card is folded to one line (absent: unfolded).
TOKEN = re.compile('[0-9a-f]{24}')  # The token a card's buttons carry (form_cards.click_token).


def valid_token(token):
    """Whether ``token`` is a card token: 24 lower-case hex digits (a click's value may hold anything)."""
    return isinstance(token, str) and TOKEN.fullmatch(token) is not None


def hub_top(tab, token, view):
    """The 设置 card's tab row above the body of panel ``tab`` (cards/settings_hub.py LABELS) when its record
    holds that card (``hub`` in its view); nothing on a standalone card. The butler's pages only with its switch
    on."""
    if not view.get('hub'):
        return []
    return [tab_row(tab, token, hub_tabs())]


def hub_tabs():
    """The 设置 card's tabs: 配置 always (it preselects the launch card too; its 管家 part only unless the deployment
    rules the butler out, COVEY_BUTLER=0), a retired one never (RETIRED); 记忆 only with
    COVEY_MEMORY=1 (core/memory.py); 设备 only with COVEY_NODE_LISTEN (core/nodes.py); 能做什么 and 诊断 always. 代答 is
    never a tab: the bot menu has its own item."""
    return [t for t in LABELS if t not in RETIRED and t != PROXY
            and (t != NODES or nodes.enabled()) and (t != MEMORY or memory.enabled())]


class MenuPanel:
    def __init__(self, via, kind, folder, lock_name, view_field='view', tab=''):
        """``kind`` names the card in its token, its idempotency keys and its log lines; ``folder`` is a path
        under the state directory, or a function of the state directory. ``tab``: the 设置 card's tab this
        panel is (cards/settings_hub.py LABELS), or a tuple of the tabs it draws, the first the one a record without
        a tab is on; '' for a panel that is none."""
        self.via, self.kind, self.folder, self.lock_name, self.view_field = via, kind, folder, lock_name, view_field
        self.tabs = (tab,) if isinstance(tab, str) and tab else tuple(tab or ())
        self.tab = self.tabs[0] if self.tabs else ''

    def directory(self, ctx):
        return self.folder(ctx.state_dir) if callable(self.folder) else ctx.state_dir / self.folder

    def path(self, ctx, token):
        """The record of the card ``token`` names: a 设置 card's when there is one (a tab's buttons carry its
        token), else this panel's own."""
        hub = ctx.state_dir / HUB_FOLDER / (token + '.json')
        return hub if self.tab and hub.exists() else self.directory(ctx) / (token + '.json')

    def field(self, path):
        """Where the record at ``path`` holds its view: a 设置 card's in ``view``, whichever panel's tab it shows."""
        return 'view' if path.parent.name == HUB_FOLDER else self.view_field

    def lock(self, path):
        """The lock its record at ``path`` changes under: a 设置 card's one lock, whichever tab it shows."""
        return resource_lock(HUB_LOCK if path.parent.name == HUB_FOLDER else self.lock_name, path)

    def shows(self, record):
        """Whether ``record`` is open on this panel: its own, or a 设置 card's on its tab."""
        return record.get('status') == OPEN and self.on_tab(record)

    def on_tab(self, record):
        return not self.tabs or record.get('tab', self.tab) in self.tabs

    def token(self, chat_id, source):
        """The token of the card opened (or adopted) for the click ``source`` in ``chat_id``."""
        return click_token(f'{self.kind}:{chat_id}:{source}')

    def open(self, ctx, chat_id, source, draw, failed, *, view=lambda token: {'revision': 1}, fields=None,
             opened=None, shown=None, quote=''):
        """Send a new card to ``chat_id`` for the menu click ``source`` (its event_id, which keys the card: a click
        delivered twice opens one). ``view(token)``: its first view; ``fields()``: more of its record, read
        before the lock (Feishu calls); ``draw(token, record)``: the card; ``opened(token)``: under the lock
        once it is out; ``shown`` hears its message id outside the lock (it recalls the card before, a Feishu
        call); ``failed``: the text sent when it did not go out. ``quote``: a message the card quotes at the chat's
        top level (not in its thread)."""
        if not chat_id or not source:
            return
        token = self.token(chat_id, source)
        path = self.path(ctx, token)
        more = {}
        if fields is not None:
            if path.exists():
                return
            more = fields()
        via, reply = self.via, {'reply_to_message_id': quote, 'in_thread': False} if quote else {}
        with self.lock(path):
            if path.exists():
                return
            record = {'schema_version': 1, 'chat_id': chat_id, 'source_event_id': source, 'status': 'publishing',
                      self.view_field: view(token), **more}
            _, mid = publish_form(path, record, lambda: via.messenger.create_and_send_card(
                ctx.profile, chat_id, draw(token, record), via.voice.key(self.kind, source), voice=via.voice, **reply),
                ready=OPEN)
            if mid and opened:
                opened(token)
        if mid:
            if shown:
                shown(mid)
            return
        via.messenger.send_message(ctx.profile, chat_id, failed, via.voice.key(self.kind + '-failed', source),
                                   voice=via.voice)

    def clicked(self, path, event, statuses=(OPEN,)):
        """The card's record when ``event`` is a click on it (its chat and message, a status of ``statuses``, a
        view with a revision, on this panel's tab when it is a 设置 card's); None otherwise."""
        try:
            record = read_json(path)
        except (OSError, ValueError):
            return None
        if (not isinstance(record, dict) or record.get('status') not in statuses or not record.get('card_id')
                or record.get('message_id') != event.get('message_id') or record.get('chat_id') != event.get('chat_id')
                or not isinstance((record.get(self.field(path)) or {}).get('revision'), int) or not self.on_tab(record)):
            return None
        return record

    def entry(self, ctx, event, value, unfolded=False):
        """The prelude of a click on a panel's card: (token, path, record) when ``value`` carries a card token and
        ``event`` is a click on that card (``clicked``; ``unfolded``: and the card is not folded); None otherwise, the
        panel's ``*_unknown`` outcome. The record is read again under the lock before it is changed."""
        token = value.get('token')
        if not valid_token(token):
            return None
        path = self.path(ctx, token)
        record = self.clicked(path, event)
        if record is None or (unfolded and record.get(FOLDED)):
            return None
        return token, path, record

    def refuse(self, ctx, path, event, notice, draw):
        """The card ``event`` clicked drawn again (``draw(view, record)``) with ``notice`` on it: a refusal said
        on the card. Whether it was this panel's card."""
        with self.lock(path):
            record = self.clicked(path, event)
            if record is None:
                return False
            view = {**record[self.field(path)], 'notice': notice}
            view['revision'] += 1
            self.publish(ctx, path, record, view, draw(view, record))
            return True

    def publish(self, ctx, path, record, view, card):
        """The whole card drawn for ``view``, saved with it; False when CardKit did not confirm (the view is saved
        anyway: the next click redraws it)."""
        changed = {**record, self.field(path): view}
        changed.pop(FOLDED, None)
        return settle(ctx, path, record, changed, card, self.via.messenger, self.kind)

    def held(self, path, chat_id, message_id, statuses=(OPEN,)):
        """The record at ``path`` when it holds the card ``message_id`` in ``chat_id``, sent and open (``statuses``),
        with a view to redraw; None otherwise (no record: a card from before it had one)."""
        return self.clicked(path, {'chat_id': chat_id, 'message_id': message_id}, statuses) if message_id else None

    def flip(self, ctx, path, record, draw, folded, *, view, fields=None, more=None):
        """Under the record's lock: the card of ``record`` folded (``folded(token, record)`` its card), or, when it
        is folded, drawn whole (``draw``) from ``view(token)`` with ``fields`` (read before the lock) in its record.
        ``more``: other fields the write saves. ('fold' or 'unfold', whether CardKit confirmed)."""
        token, fold, field = path.stem, not record.get(FOLDED), self.field(path)
        revision = record[field]['revision'] + 1
        changed = {**record, **({} if fold else fields or {}), **(more or {}),
                   field: {**(record[field] if fold else view(token)), 'revision': revision}}
        if fold:
            changed[FOLDED] = True
        else:
            changed.pop(FOLDED, None)
        card = folded(token, changed) if fold else draw(token, changed)
        return ('fold' if fold else 'unfold'), settle(ctx, path, record, changed, card, self.via.messenger, self.kind)

    def toggle(self, ctx, chat_id, source, message_id, draw, folded, *, view=lambda token: {'revision': 1},
               fields=None, shown=None):
        """A menu click on the card the click ``source`` opened in ``chat_id`` (its message ``message_id``), the newest
        there: it folds, or unfolds when folded (``flip``; ``fields()`` is read before the lock only to unfold,
        ``shown(token)`` hears an unfolded card). 'fold' or 'unfold'; '' when this card has no open record
        (sent before records, gone, closed): the menu sends a new card instead."""
        path = self.path(ctx, self.token(chat_id, source))
        record = self.held(path, chat_id, message_id)
        if record is None:
            return ''
        more = fields() if fields is not None and record.get(FOLDED) else {}
        with self.lock(path):
            record = self.held(path, chat_id, message_id)
            if record is None:
                return ''
            outcome, confirmed = self.flip(ctx, path, record, draw, folded, view=view, fields=more)
            if outcome == 'unfold' and shown:
                shown(path.stem)
        self.via.messenger.log(f'{self.kind} card {outcome} token={path.stem[:8]} confirmed={confirmed}')
        return outcome

    def unsaved(self, ctx, record, text, suffix, **reply):
        """``text`` in the card's chat: what happened, which the card could not be redrawn to show."""
        voice = self.via.voice
        self.via.messenger.send_message(ctx.profile, record['chat_id'], text, voice.key(self.kind + '-unsaved', suffix),
                                        voice=voice, **reply)
