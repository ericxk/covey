"""The 设置 card's 脱敏 tab: the redaction term list (core/redaction.py) kept on the card itself, one layer
(cards/redaction_panel.py): 填词 and the list to find, add and delete terms, and folds for the switches and the
upstream address with 探测, 试一下, and 记录 of what the relays counted. Stand-ins are automatic (the owner's decision
of 2026-11-14): no 改, no way to name one; a button of the 2026-11-13 layout still answers (RETIRED). The web dashboard (app/dashboard.py), when the deployment serves
one, is its last part: the owner moved the list here on 2026-11-13, when the browser could not reach the port.

The tab's record is the 设置 card's (app/settings_hub.py); a card older than that one record (gen 286) had one in
LINK_FOLDER, which the 设置 card's takeover still reads through this panel. A click (app/unified.py PANELS, the
owner's) changes the list through ``update_config`` outside the record's lock, then the whole card is drawn again
under it with what happened (``notice``); 探测 runs off the click (``start``) and draws the card again when it ends.
The same click delivered again acts once: the view keeps the last ``seen`` event ids.

No term goes on the card, into its record or into a log line: the card is drawn from ``facts``, which names a term by
core/redaction.py ``masked`` and by ``row_key``, an HMAC under a key of the state directory (KEY_FILE, 0600), so a
button's value says nothing a guess could be tested against; a search keeps the keys it found, not what was typed;
撤销 keeps the deleted row in this process's memory only; a refusal is a core/redaction.py ``Refused``, whose words
name no term, and a list that cannot be read is UNREADABLE, since its error may quote one.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time
from pathlib import Path

from covey.app.card_actions import form_fields
from covey.app.dashboard import LINK_FOLDER
from covey.app.menu_panel import FOLDED, MenuPanel, hub_top
from covey.cards.redaction_panel import (ADD, FIND, KINDS, LINES, LIST, OPEN, PROBE, QUERY, RETIRED, RETIRED_WORDS, SWITCH,
                                         SWITCH_ROWS, TERM, TEXT, UNDO, UPSTREAM, UPSTREAM_FIELD, redaction_card)
from covey.cards.settings_hub import REDACTION
from covey.core.redaction import (Refused, config_path, load_config, masked, normalize, row_problem, term_lines,
                                  update_config, upstream_problem)
from covey.core.redaction_views import MAX_PREVIEW, restore_tag, stats_view
from covey.core.redactor import Redactor
from covey.core.state import epoch_seconds, read_object, write_private_text
from covey.core.text import literal

KEY_FILE = 'card-key'  # <state>/redaction/KEY_FILE: the key ``row_key`` hashes a term under.
KEY_LOCK = threading.Lock()
SEEN = 8  # The last click event ids the view keeps.
FOUND = 200  # The most rows a search keeps.
SHOWN = 3000  # The most of a try-out's result the view keeps.
PROBE_STALE = 120  # Seconds after which a 探测 that never ended (the Bridge stopped) no longer holds the button.
OWNER_ONLY = '⚠️ 仅机器人主人可以改敏感词。'
GONE = '这个词已经不在词表里了，卡片按最新的词表重画了。'
UNREADABLE = '词表文件读不了或有错。细节里可能有原词，卡片上不显示：请查看状态目录里的 redaction/config.json。'
CASE_CLASH = '打开后词表里会有只差大小写的重复词，或替身词含有别的词：先在词表里改掉再打开。'
UNSAVED = '\n设置卡没能刷新：再点机器人菜单「设置」看「脱敏」。'

# 新话题默认开 is the 新话题 page's 脱敏 row now (cards/redaction_panel.py SWITCH_ROWS): an older card's switch says so.
MOVED_SWITCH = 'default_on'
MOVED_NOTICE = '「新话题默认开」挪到了设置卡「新话题」页的「脱敏」一行。'


def card_key(state_dir) -> bytes:
    """The key ``row_key`` hashes a term under, made on first use."""
    path = Path(state_dir) / 'redaction' / KEY_FILE
    with KEY_LOCK:
        try:
            key = path.read_text(encoding='utf-8').strip()
        except FileNotFoundError:
            key = ''
        if len(key) < 32:
            key = secrets.token_hex(32)
            write_private_text(path, key + '\n')
    return key.encode()


def row_key(key, term) -> str:
    """A button's name for the row of ``term``: 12 hex digits of an HMAC under ``key``."""
    return hmac.new(key, term.encode('utf-8'), hashlib.sha256).hexdigest()[:12]


def index_of(config, key, wanted):
    """The index of the row ``row_key`` names ``wanted``; None when none is."""
    return next((i for i, row in enumerate(config['terms']) if row_key(key, row['term']) == wanted), None)


def when(seconds) -> str:
    return time.strftime('%m-%d %H:%M', time.localtime(seconds)) if seconds else ''


def facts(state_dir, key, now=None) -> dict:
    """What the tab draws, no term in it: the switches and the upstream address, a row a term (newest first: its masked
    name, row key, stand-in if one is set, match, restore tag and hits), the recorded turns' figures, the topics with
    redaction on among those they name and the latest turns; ``error`` when the list cannot be read."""
    stats = stats_view(state_dir, time.time() if now is None else now)
    hits = {row['term']: row for row in stats['terms']}
    data = {'error': '', 'config': {}, 'rows': [], 'count': 0, 'week': stats['week_hits'], 'updated': '',
            'topics': [{'number': t.get('number'), 'title': t.get('title', ''), 'turns': t.get('turns', 0),
                        'hits': t.get('hits', 0), 'last': when(t.get('last'))} for t in stats['topics']],
            'turns': [{'time': when(t.get('ts')), 'number': t.get('number'), 'model': str(t.get('model') or ''),
                       'requests': t.get('requests', 0), 'replaced': t.get('replaced', 0)} for t in stats['turns']]}
    try:
        config = load_config(state_dir)
    except ValueError:
        return {**data, 'error': UNREADABLE}
    redactor = Redactor(config)
    data['config'] = {name: config[name] for name in (*SWITCH_ROWS, 'upstream')}
    data['rows'] = [{'key': row_key(key, row['term']), 'label': masked(row['term']), 'stand_in': row['stand_in'],
                     'anywhere': row['anywhere'],
                     'restore': restore_tag(config, redactor, row['term']),
                     'hits': hits.get(row['term'], {}).get('hits', 0), 'week': hits.get(row['term'], {}).get('week', 0)}
                    for row in reversed(config['terms'])]
    data['count'] = len(config['terms'])
    data['updated'] = when(epoch_seconds(read_object(config_path(state_dir)).get('updated_at')))
    return data


def skipped_words(skipped):
    """The lines 加入词表 left out, by reason: 第 2、5 行没加（词表里已经有这个词）."""
    lines = {}
    for number, why in skipped:
        lines.setdefault(why, []).append(number)
    return '；'.join(f"第 {'、'.join(map(str, numbers[:6]))}{' 等' if len(numbers) > 6 else ''} 行没加（{why}）"
                    for why, numbers in lines.items())


class RedactionPanel:
    def __init__(self, runtime, messenger, voice, dashboard, *, start=None):
        """``dashboard``: the web dashboard (app/dashboard.py), whose link is the tab's last part when it is served
        and whose 探测 is the tab's; ``start(target)`` runs 探测 off the click (a daemon thread)."""
        self.runtime, self.messenger, self.voice, self.dashboard = runtime, messenger, voice, dashboard
        self.menu = MenuPanel(self, 'redaction-panel', LINK_FOLDER, 'dashboard-link', tab=REDACTION)
        self.removed = {}  # A card's token -> (the row its last 删除 took, its index): for 撤销, in memory only.
        self.removed_lock = threading.Lock()
        self.start = start or (lambda target: threading.Thread(target=target, name='redaction-probe',
                                                               daemon=True).start())

    def path(self, ctx, token):
        return self.menu.path(ctx, token)

    @staticmethod
    def matches(value):
        return value.get('kind') in KINDS

    def claim(self, ctx, event, raw, owner):
        """The click's outcome when it is the tab's, else None (app/unified.py PANELS)."""
        return self.click(ctx, event, raw, owner) if self.matches(raw) else None

    # The 设置 card's 脱敏 tab (app/settings_hub.py): its first view, its record's fields, its card, once shown.
    @staticmethod
    def tab_view(token):
        return {'token': token, 'revision': 1, 'hub': True, 'at': 0}

    @staticmethod
    def tab_fields(ctx):
        return {}

    def tab_card(self, ctx, token, record):
        return self.draw(ctx, token, record['view'], str(record.get('chat_id') or ''))

    def tab_shown(self, ctx, token):
        self.runtime.log(f'redaction tab shown token={token[:8]}')

    def refuse(self, ctx, path, event):
        """OWNER_ONLY on the card ``event`` clicked; whether it was this tab's."""
        return self.menu.refuse(ctx, path, event, OWNER_ONLY,
                                lambda view, record: self.draw(ctx, path.stem, view, str(record.get('chat_id') or '')))

    def rotate_token(self, ctx):
        """换口令 in the tab's web part (SettingsHub.rotate)."""
        self.dashboard.rotate_token(ctx)

    def draw(self, ctx, token, view, chat_id=''):
        """The tab for ``view``; ``chat_id``, the owner's private chat, is where the web dashboard's link goes when its
        address moves (Dashboard.web_link)."""
        return redaction_card(token, view, facts(ctx.state_dir, card_key(ctx.state_dir)),
                              top=hub_top(REDACTION, token, view), web=self.dashboard.web_link(ctx, chat_id))

    def click(self, ctx, event, value, owner):
        """A click on the tab; the outcome its ``card action`` line records."""
        token = value.get('token')
        if not isinstance(token, str) or not re.fullmatch('[0-9a-f]{24}', token):
            return 'redaction_unknown'
        path = self.path(ctx, token)
        record = self.menu.clicked(path, event)
        if record is None or record.get(FOLDED):
            return 'redaction_unknown'
        if not owner:
            self.refuse(ctx, path, event)
            return 'owner_only'
        event_id = str(event.get('event_id') or '')
        if event_id and event_id in (record['view'].get('seen') or ()):
            return 'duplicate'
        changes = self.act(ctx, token, value, event, record['view'])
        with self.menu.lock(path):
            record = self.menu.clicked(path, event)  # Read again: another click may have drawn it since.
            if record is None or record.get(FOLDED):
                return 'redaction_unknown'
            view = {**record['view'], 'notice': '', 'undo': False, 'rotated': False, 'preview': None, **changes,
                    'token': token}  # A try-out's result stays until the next click.
            view['seen'] = [*(view.get('seen') or ())][-(SEEN - 1):] + ([event_id] if event_id else [])
            view['revision'] += 1
            confirmed = self.menu.publish(ctx, path, record, view,
                                          self.draw(ctx, token, view, str(record.get('chat_id') or '')))
        if not confirmed and view['notice']:
            self.menu.unsaved(ctx, record, view['notice'] + UNSAVED, f"{token}-{view['revision']}")
        if changes.get('probing'):
            self.start(lambda: self.probe(ctx, path, event))
        return 'redaction_tab'

    def act(self, ctx, token, value, event, view):
        """The view's changes for the click ``value``, the list changed first."""
        kind, fields = value['kind'], form_fields(event) or {}
        try:
            if kind in RETIRED:  # An older layout's button: the card is drawn again in the new one.
                return {'notice': RETIRED_WORDS, 'edit': '', 'page': None}
            if kind == OPEN:
                return self.opened(value, view)
            if kind == LIST:
                return self.turn(value)
            if kind == FIND:
                return self.find(ctx, fields.get(QUERY))
            if kind == ADD:
                return self.add(ctx, fields.get(LINES))
            if kind == TERM:
                return self.term(ctx, token, value, view)
            if kind == UNDO:
                return self.undo(ctx, token)
            if kind == SWITCH:
                return self.switch(ctx, value)
            if kind == UPSTREAM:
                return self.upstream(ctx, value, fields.get(UPSTREAM_FIELD), view)
            if kind == PROBE:
                return self.probing(view)
            return self.preview(ctx, fields.get(TEXT))
        except Refused as exc:
            return {'notice': '⚠️ ' + str(exc)}
        except ValueError:  # The saved list cannot be read; what it says may quote a term.
            return {'notice': '⚠️ ' + UNREADABLE}
        except OSError as exc:
            return {'notice': f'⚠️ 没有保存（{type(exc).__name__}）'}

    @staticmethod
    def opened(value, view):
        """试一下 or the upstream address's box opened or closed (``open``: the names open)."""
        name, on = value.get('name'), value.get('on')
        if name not in ('try', 'upstream') or not isinstance(on, bool):
            return {}
        names = [n for n in (view.get('open') or ()) if n != name]
        return {'open': names + [name] if on else names, 'preview': view.get('preview') if name == 'try' else None}

    @staticmethod
    def turn(value):
        """A page of the list (``at``); ``clear`` ends the search."""
        at = value.get('at', 0)
        if not isinstance(at, int) or isinstance(at, bool):
            return {}
        return {'found': None, 'at': 0} if value.get('clear') else {'at': max(at, 0)}

    @staticmethod
    def find(ctx, query):
        """查找: the rows whose term or stand-in holds ``query``, kept as their keys."""
        query = str(query or '').strip()
        if not query:
            return {'notice': '在搜索框里输入原词或替身词，再点“查找”。'}
        config = load_config(ctx.state_dir)
        fold = str.casefold if config['ignore_case'] else str
        key, wanted = card_key(ctx.state_dir), fold(query)
        found = [row_key(key, row['term']) for row in reversed(config['terms'])
                 if wanted in fold(row['term']) or wanted in fold(row['stand_in'])]
        return {'found': found[:FOUND], 'at': 0}

    @staticmethod
    def add(ctx, text):
        """加入词表: each line a term, checked against the list and the lines before it; a line written
        ``原词 => 替身词`` (the 2026-11-13 way) is left out."""
        rows = term_lines(text)
        if not rows:
            return {'notice': '先在框里填词：每行一个。'}

        def change(config):
            added, skipped = [], []
            for number, term in rows:
                why = '替身词自动生成，不用写 =>' if '=>' in term else row_problem(
                    config['terms'], term, '', ignore_case=config['ignore_case'])
                if why:
                    skipped.append((number, why))
                    continue
                config['terms'].append({'term': term, 'stand_in': '', 'anywhere': False})
                added.append(term)
            if not added:
                raise Refused('一个也没加：' + skipped_words(skipped))
            return added, skipped

        added, skipped = update_config(ctx.state_dir, change)
        named = '、'.join(f'「{literal(masked(term))}」' for term in added[:5])
        notice = f"✓ 加入了 {named}" + (f' 等 {len(added)} 个词' if len(added) > 5 else '')
        return {'at': 0, 'found': None, 'notice': notice + (f'；{skipped_words(skipped)}' if skipped else '') + '。'}

    def term(self, ctx, token, value, view):
        """A row's 删除 (撤销 puts it back); the old 改 is retired."""
        wanted, op = value.get('key'), value.get('op')
        if op == 'edit':
            return {'notice': RETIRED_WORDS}
        if not isinstance(wanted, str) or op != 'delete':
            return {}
        key = card_key(ctx.state_dir)

        def change(config):
            index = index_of(config, key, wanted)
            if index is None:
                raise Refused(GONE)
            return config['terms'].pop(index), index

        row, index = update_config(ctx.state_dir, change)
        with self.removed_lock:
            self.removed[token] = (row, index)
        return {'notice': f"已删除「{literal(masked(row['term']))}」。", 'undo': True}

    def undo(self, ctx, token):
        """撤销: the row the card's last 删除 took, back where it was."""
        with self.removed_lock:
            entry = self.removed.pop(token, None)
        if entry is None:
            return {'notice': '没有能撤销的删除了（已经撤销过，或 Bridge 重启过）。'}
        row, index = entry

        def change(config):
            why = row_problem(config['terms'], row['term'], row['stand_in'], ignore_case=config['ignore_case'])
            if why:
                raise Refused('没能放回：' + why)
            config['terms'].insert(min(index, len(config['terms'])), row)

        update_config(ctx.state_dir, change, allow=[row])  # An older version's stand-in comes back whole.
        return {'notice': f"✓ 放回了「{literal(masked(row['term']))}」。"}

    @staticmethod
    def switch(ctx, value):
        """不区分大小写 or 回复还原 set; an older card's 新话题默认开 says where it is now."""
        name, on = value.get('name'), value.get('on')
        if name == MOVED_SWITCH:
            return {'notice': MOVED_NOTICE}
        if name not in SWITCH_ROWS or not isinstance(on, bool):
            return {}
        label = SWITCH_ROWS[name][0]
        if load_config(ctx.state_dir)[name] == on:
            return {'notice': f"「{label}」已经是{'开' if on else '关'}的了。"}

        def change(config):
            config[name] = on
            try:
                normalize(config)  # 不区分大小写 may make two terms one: say so without them.
            except ValueError:
                raise Refused(CASE_CLASH) from None

        update_config(ctx.state_dir, change)
        return {'notice': f"✓「{label}」{'打开' if on else '关掉'}了。"}

    @staticmethod
    def upstream(ctx, value, typed, view):
        """上游地址 saved or cleared."""
        op = value.get('op')
        if op not in ('save', 'clear'):
            return {}
        address = '' if op == 'clear' else str(typed or '').strip().rstrip('/')
        why = upstream_problem(address)
        if why:
            raise Refused(why)

        def change(config):
            config['upstream'] = address

        update_config(ctx.state_dir, change)
        return {'probe': None, 'open': [n for n in view.get('open') or () if n != 'upstream'], 'notice': f'✓ 上游地址改成 {literal(address)}。' if address else '✓ 上游地址清空了：每轮自动探测。'}

    @staticmethod
    def preview(ctx, text):
        """试一下: what ``text`` goes out as with the saved list, how many places, whether it turns back."""
        text = str(text or '')
        if not text.strip():
            return {'preview': None, 'notice': '先在框里粘贴一段文字。'}
        if len(text) > MAX_PREVIEW:
            raise Refused(f'要试的文字最多 {MAX_PREVIEW} 个字')
        config = load_config(ctx.state_dir)
        redactor = Redactor(config)
        redacted = redactor.redact_text(text)
        return {'preview': {'redacted': redacted[:SHOWN], 'count': sum(redactor.hits.values()),
                            'terms': len(redactor.hits), 'restore': config['restore'],
                            'same': redactor.restore_text(redacted) == text}}

    @staticmethod
    def probing(view):
        """探测 starts (``probing``: when; the click starts it), unless one started and has not ended."""
        started = view.get('probing') or 0
        if isinstance(started, (int, float)) and time.time() - started < PROBE_STALE:
            return {'notice': '上一次探测还没结束。'}
        return {'probing': time.time(), 'probe': None}

    def probe(self, ctx, path, event):
        """探测, off the click: where a turn's requests would go, drawn on the card while it shows the tab."""
        try:
            status, body = self.dashboard.http.detect_now()
            result = ({'upstream': str(body.get('upstream') or ''), 'source': str(body.get('source') or '')}
                      if status == 200 else {'error': str(body.get('error') or '探测失败')})
            with self.menu.lock(path):
                record = self.menu.clicked(path, event)
                if record is None or record.get(FOLDED) or not record['view'].get('probing'):
                    return
                view = {**record['view'], 'probing': 0, 'probe': result, 'notice': ''}
                view['revision'] += 1
                confirmed = self.menu.publish(ctx, path, record, view,
                                              self.draw(ctx, path.stem, view, str(record.get('chat_id') or '')))
            self.runtime.log(f'redaction tab probe status={status} token={path.stem[:8]} confirmed={confirmed}')
        except Exception as exc:  # noqa: BLE001 - a thread of its own: the next 探测 tries again.
            self.runtime.log(f'redaction tab probe failed ({type(exc).__name__})')
