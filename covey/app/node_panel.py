"""The 设置 card's 设备 tab (cards/node_panel.py), shown when COVEY_NODE_LISTEN is set (core/nodes.py): the nodes
and their state now, 添加 and 移除.

The tab's record is the 设置 card's (app/settings_hub.py, app/menu_panel.py); a click (app/unified.py PANELS, the
owner's) acts outside the record's lock, then the whole card is drawn again under it (``notice`` says what happened).
- **添加** opens a join (core/nodes.py ``open_join``): without an ssh address the card shows the line to run on the
  device (app/node_gateway.py ``paste_command``); with one, the Hub runs that line over ssh off the click (``start``),
  then draws the card again with the outcome. The ssh address is split like a shell would and passed as arguments,
  never through a shell; options that make ssh run a local command are refused.
- **移除** asks first (``confirm``); confirmed, the node is revoked (``revoke``; a connected one is told at once, the
  gateway's ``revoke_now``) and uninstalls itself.
- **刷新** draws the card again.
The same click delivered again acts once: the view keeps the last ``seen`` event ids.
"""
from __future__ import annotations

import subprocess
import threading
import time

from covey.app.card_actions import form_fields
from covey.app.menu_panel import FOLDED, MenuPanel, hub_top
from covey.app.node_gateway import paste_command
from covey.cards.node_cards import machine_lines
from covey.cards.node_panel import ADD, LABEL, NAME, REFRESH, REMOVE, SSH, KINDS, node_panel_card
from covey.cards.settings_hub import NODES
from covey.core import devices, nodes

SEEN = 8
WITH_VALUE = devices.SSH_WITH_VALUE  # ssh options that take the next word (core/devices.py).
SSH_SECONDS = 90
OWNER_ONLY = '⚠️ 仅机器人主人可以管理设备。'
UNSAFE_SSH = devices.SSH_UNSAFE  # Run something here.


def ssh_args(text):
    """The ssh arguments for the address the owner typed; ValueError when it cannot be one (core/devices.py ``ssh_words``)."""
    return devices.ssh_words(text)


def facts(state_dir, address, now=None):
    """The rows of the card: the hub's own node first, then the others by name."""
    now = time.time() if now is None else now
    rows = []
    active = nodes.active(state_dir)
    for alias in sorted(active, key=lambda a: (a != nodes.LOCAL, a)):
        entry = active[alias]
        link, latest = nodes.read_link(state_dir, alias), nodes.read_latest(state_dir, alias)
        title = '本机' if alias == nodes.LOCAL else (entry.get('title') or alias)
        if alias != nodes.LOCAL and title != alias:
            title = f'{title}（{alias}）'
        line, grey = machine_lines(title, link, latest, online=nodes.online(link, now), now=now)
        rows.append({'alias': alias, 'title': title, 'line': line, 'grey': grey, 'local': alias == nodes.LOCAL})
    return {'address': address, 'rows': rows, **device_summary()}


ACCESS_WORDS = {'write': '可写', 'read': '只读'}


def device_summary():
    """The card's one line on devices.json, with devices on (COVEY_DEVICES=1): ``devices`` (每台的名字和可写 / 只读) and ``manage``
    (the web page's 设备, with the page on): the card only shows it, the page changes it. {} with devices off."""
    if not devices.enabled():
        return {}
    try:
        config = devices.current()
    except devices.DeviceConfigError:
        return {'devices': 'devices.json 有问题，先手工改好'}
    found = [*config.devices.items(), *config.watched.items()]
    words = [f"{device.title}（{ACCESS_WORDS.get(device.access, '没设可写还是只读') if device.host else '只能看状况'}）"
             for _, device in sorted(found)]
    from covey.core import web_sync
    url = web_sync.page_url()
    return {'devices': '设备名单：' + ('、'.join(words) if words else '还没有'), **({'manage': url.rstrip('/') + '/settings/nodes'} if url else {})}


class NodePanel:
    def __init__(self, runtime, messenger, voice, gateway, *, environ=None, start=None):
        """``gateway``: app/node_gateway.py's (its address, TLS, revoke_now); ``start(target)`` runs the ssh join off
        the click (a daemon thread)."""
        self.runtime, self.messenger, self.voice, self.gateway, self.environ = runtime, messenger, voice, gateway, environ
        self.menu = MenuPanel(self, 'node-panel', 'node-cards', 'node-card', tab=NODES)
        self.start = start or (lambda target: threading.Thread(target=target, name='node-join', daemon=True).start())

    def path(self, ctx, token):
        return self.menu.path(ctx, token)

    @staticmethod
    def matches(value):
        return value.get('kind') in KINDS

    def claim(self, ctx, event, raw, owner):
        return self.click(ctx, event, raw, owner) if self.matches(raw) else None

    @staticmethod
    def tab_view(token):
        return {'token': token, 'revision': 1, 'hub': True}

    @staticmethod
    def tab_fields(ctx):
        return {}

    def tab_card(self, ctx, token, record):
        return self.draw(ctx, token, record['view'])

    def tab_shown(self, ctx, token):
        self.runtime.log(f'nodes tab shown token={token[:8]}')

    def refuse(self, ctx, path, event):
        return self.menu.refuse(ctx, path, event, OWNER_ONLY, lambda view, record: self.draw(ctx, path.stem, view))

    def draw(self, ctx, token, view):
        return node_panel_card(token, view, facts(ctx.state_dir, self.gateway.address), top=hub_top(NODES, token, view))

    def click(self, ctx, event, value, owner):
        entry = self.menu.entry(ctx, event, value, unfolded=True)
        if entry is None:
            return 'nodes_unknown'
        token, path, record = entry
        if not owner:
            self.refuse(ctx, path, event)
            return 'owner_only'
        event_id = str(event.get('event_id') or '')
        if event_id and event_id in (record['view'].get('seen') or ()):
            return 'duplicate'
        changes, later = self.act(ctx, value, event, record['view'])
        if not self.redraw(ctx, path, event, changes, event_id):
            return 'nodes_unknown'
        if later:
            self.start(lambda: later(path, event))
        return 'nodes_tab'

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

    def act(self, ctx, value, event, view):
        """(the view's changes, a function to run off the click or None)."""
        kind = value.get('kind')
        if kind == REFRESH:
            return {}, None
        if kind == REMOVE:
            alias, op = value.get('alias'), value.get('op')
            if not isinstance(alias, str) or alias == nodes.LOCAL:
                return {}, None
            if op == 'ask':
                return {'confirm': alias}, None
            if op == 'cancel':
                return {'confirm': None}, None
            if op == 'confirm' and nodes.revoke(ctx.state_dir, alias):
                self.gateway.revoke_now(alias)
                self.runtime.log(f'node {alias} removed from the 设备 tab')
                return {'confirm': None, 'notice': f'已移除 {alias}：它在线时马上卸载自己，不在线时下次连上来再卸载。'}, None
            return {'confirm': None, 'notice': f'{alias} 已经不在了。'}, None
        if kind == ADD:
            return self.add(ctx, form_fields(event) or {})
        return {}, None

    def add(self, ctx, fields):
        if self.gateway.tls is None:
            return {'notice': '⚠️ Hub 还没开始监听节点，稍后再试。'}, None
        alias = str(fields.get(NAME) or '').strip().lower()
        title = str(fields.get(LABEL) or '').strip()[:40]
        typed = str(fields.get(SSH) or '').strip()
        try:
            args = ssh_args(typed) if typed else None
            token = nodes.open_join(ctx.state_dir, alias, title or alias, via='ssh' if args else 'paste')
        except ValueError as exc:
            return {'notice': '⚠️ ' + str(exc)}, None
        command = paste_command(self.gateway.address, self.gateway.tls.spki, token)
        until = time.strftime('%H:%M', time.localtime(time.time() + nodes.JOIN_TTL))
        self.runtime.log(f'node join opened alias={alias} via={"ssh" if args else "paste"}')
        if not args:
            return {'adding': {'alias': alias, 'command': command, 'until': until}}, None

        def join(path, event):
            notice = self.ssh_join(alias, args, command)
            self.redraw(ctx, path, event, {'notice': notice, 'adding': None})

        return {'adding': None, 'notice': f'⏳ 正在经 ssh 登上 {alias} 安装节点（最多 {SSH_SECONDS} 秒）…'}, join

    def ssh_join(self, alias, args, command):
        try:
            out = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', *args, command],
                                 capture_output=True, text=True, timeout=SSH_SECONDS, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return f'⚠️ 经 ssh 安装 {alias} 超时。可以改用不填 ssh 的方式，在设备上自己执行那行命令。'
        except OSError as exc:
            return f'⚠️ 没能运行 ssh：{type(exc).__name__}'
        tail = (out.stdout.strip().splitlines() or [''])[-1]
        self.runtime.log(f'node ssh join alias={alias} rc={out.returncode}')
        if out.returncode == 0 and '"ok": true' in tail:
            return f'✅ {alias} 已装好节点并连上 Hub，之后不再经 ssh。'
        reason = (out.stderr.strip().splitlines() or [tail or f'退出码 {out.returncode}'])[-1][:200]
        return f'⚠️ 经 ssh 安装 {alias} 没成功：{reason}'
