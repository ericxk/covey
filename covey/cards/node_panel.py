"""The 设置 card's 设备 tab (app/node_panel.py): every node with its state now, adding a device, removing one.

One layer: the Hub's address on top; a row a node (cards/node_cards.py ``machine_lines``: 🟢 gpu1 · CPU 12% · 内存 41% ·
盘 87%, the grey line under it), the hub's own first, then the devices, each but the hub's with 移除 (two steps: 移除,
then 确认移除 or 取消); then 添加设备: a form with the device's name, an optional title and an optional ssh address.
添加 without an ssh address shows the one line to run on the device (``command``, 30 minutes); with one, the Hub runs
that line over ssh itself and the card says how it went. 刷新 draws the card again with the figures of now.

Each button's value carries its kind, the card's token and the revision drawn.
"""
from __future__ import annotations

from covey.cards.elements import callback_button, item, md, note, panel_header, plain, styled_panel, text_button
from covey.cards import ui
from covey.core.text import safe_text

TITLE = '设备'
ADD = 'bridge_nodes_add'  # 添加: the form's NAME, LABEL and SSH.
REMOVE = 'bridge_nodes_remove'  # A row's 移除 (``alias``); ``op`` ask, confirm or cancel.
REFRESH = 'bridge_nodes_refresh'
KINDS = frozenset({ADD, REMOVE, REFRESH})
NAME, LABEL, SSH = 'node_name', 'node_title', 'node_ssh'
NAME_HINT = '名字：小写字母开头，只用小写字母、数字和 -（如 gpu1）'
ADD_NOTE = ('不填 ssh：卡上给一行命令，在设备上执行一次（30 分钟内有效）。填了 ssh（如 `user@host -p 22`）：Hub 替你登上去执行，'
            '之后不再经 ssh。设备只需要 python3 和 curl，不开端口。')
NOTE = '每 10 秒更新一次；卡片不会自己刷新，点「刷新」看最新。内存、OOM、盘满或写不进、失联会单独发告警卡。'


def submit(label, value, name, *, primary=False):
    button = callback_button(label, value, style='primary_filled' if primary else 'default', size='small')
    button.update(name=name, form_action_type='submit')
    return button


def box(name, placeholder, *, limit=200):
    return {'tag': 'input', 'name': name, 'width': 'fill', 'max_length': limit, 'placeholder': plain(placeholder)}


def node_panel_card(token, view, data, *, top=()):
    """The tab for ``view`` (revision, notice, adding, confirm) over ``data`` (app/node_panel.py ``facts``: address,
    rows of {alias, title, line, grey, local}; with devices on ``devices``, the devices.json line, and ``manage``, the web
    page's 设备)."""
    def value(kind, **more):
        return {'kind': kind, 'token': token, 'revision': view['revision'], **more}

    elements = [*top]
    if view.get('notice'):
        elements.append(ui.receipt(view['notice']))
    for row in data['rows']:
        buttons = []
        if not row['local']:
            if view.get('confirm') == row['alias']:
                buttons = [text_button('确认移除', value(REMOVE, alias=row['alias'], op='confirm'), tone='danger'),
                           text_button('取消', value(REMOVE, alias=row['alias'], op='cancel'))]
            else:
                buttons = [text_button('移除', value(REMOVE, alias=row['alias'], op='ask'), tone='danger')]
        elements.append(item(md(row['line']), *buttons))
        if row['grey']:
            elements.append(note(row['grey']))
    if not data['rows']:
        elements.append(md('还没有设备。'))
    if data.get('devices'):  # devices.json, with devices on: shown here, changed on the web page only.
        manage = f" · [去网页管理]({data['manage']})" if data.get('manage') else ' · 在网页工作台的「设置 → 设备」里管理'
        elements.append(note(safe_text(data['devices'], 300) + manage))
    adding = view.get('adding') if isinstance(view.get('adding'), dict) else None
    elements.append(md('**添加设备**'))
    if adding and adding.get('command'):
        elements.append(md(f"在 **{safe_text(adding.get('alias', ''), 24)}** 上执行（{adding.get('until', '')} 前有效）："))
        elements.append(md('```bash\n' + adding['command'] + '\n```'))
    elements.append({'tag': 'form', 'name': 'nodes_add', 'elements': [
        box(NAME, NAME_HINT, limit=24), box(LABEL, '显示名（可不填）', limit=40),
        box(SSH, 'ssh 地址（可不填），如 user@host -p 22'),
        submit('添加', value(ADD), 'add', primary=True)]})
    elements.append(note(ADD_NOTE))
    elements.append(item(note(NOTE), text_button('刷新', value(REFRESH), tone='primary')))
    subtitle = f"Hub {data['address']}" if data.get('address') else 'Hub 还没开始监听'
    return styled_panel(panel_header('computer_outlined', TITLE, subtitle), elements, TITLE)
