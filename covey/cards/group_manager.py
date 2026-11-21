"""The 群聊 card (app/group_manager.py), the bot menu's 群聊 panel: every group the bot has a record of
(app/access.py), a row each with its state as a coloured tag and a 停用 or 启用 text button (a group waiting for
the owner's word: 启用 and 先不启用, and it comes first), PAGE_SIZE a page. Each button's value carries its kind, the
card's token and the revision drawn.

With 免@ offered (``free``, app/access.py FREE_ENV) a row also shows its 免@ mode as a blue tag and a 免@ button;
that opens the group's 免@ page (VIEW): the four choices of who talks there without the @ (FREE), and with
你和选中的人 the group's members, MEMBER_PAGE a page, each picked or dropped (MEMBER); 返回群列表 goes back.

The confirmation card (``ask_card``): the owner's first @ in a group that can be a team space sends it to his private
chat. It names the group, says who could use the bot there and what that means, and has 启用 and 先不启用 (CONFIRM,
with the group's chat and name); once he chose (or the group was decided on the 群聊 card) it is drawn whole as what
holds."""
from __future__ import annotations

from covey.cards import ui
from covey.core.groups import pending
from covey.core.text import literal
from covey.cards.elements import (callback_button, folded_panel, grey, item, md, menu_item, note, panel_header,
                                            row, styled_panel, tag, text_button)

SWITCH = 'bridge_group_switch'
PAGE = 'bridge_group_page'  # With ``members``: the 免@ page's members.
VIEW = 'bridge_group_view'  # A group's 免@ page (``chat``), or back to the list (``chat`` '').
FREE = 'bridge_group_free'  # A group's 免@ mode (``chat``, ``mode``: '' off or a FREE_CHOICES key).
MEMBER = 'bridge_group_member'  # One member picked or dropped (``chat``, ``member``, ``on``).
CONFIRM = 'bridge_group_confirm'  # The confirmation card's 启用 / 先不启用 (``chat``, ``on``; ``name`` to draw it again).
KINDS = frozenset({SWITCH, PAGE, VIEW, FREE, MEMBER, CONFIRM})
PAGE_SIZE = 10
MEMBER_PAGE = 8
# Who talks without the @, by app/access.py FREE_MODES ('' off): the label and what it means.
FREE_CHOICES = {
    '': ('关闭', '都要 @ 机器人，和以前一样'),
    'owner': ('只有你', '你的消息不用 @，群里其他人照旧要 @'),
    'some': ('你和选中的人', '你和下面选中的人不用 @，其他人照旧要 @'),
    'all': ('群里所有人', '每个人的每句话都会开任务，以你的全部权限运行'),
}
FREE_NOTE = ('免@ 的群里，@ 了别人（人、另一个机器人或所有人）却没 @ 机器人的消息不接。机器人能收到它所在每个群的全部消息，'
             '没开免@ 的群照旧只接 @ 它的。')
PROXY_BLOCKED = '代答在用这个群（它以你的身份发消息），不能免@。'
EXTERNAL_ONLY = '外部群只处理你的消息，只能选“只有你”。'
INACTIVE_ONLY = '这个群没有启用：只处理你的消息，启用后才能选群里的其他人。'
NO_MEMBERS = '读不到群成员：应用要开通「获取群成员」权限（im:chat.members:read）。已选的人仍然有效。'
ALONE = '群里除了你还没有别人可选：拉人进群后再点一次“免@”。'
TITLE = '群聊'
NOTE = ('已启用的群里，群成员 @ 机器人就能发起任务；停用后只处理你的消息。你第一次在一个群里 @ 机器人时，它会出现在这里，'
        '私聊里会来一张卡问你启不启用。')
EMPTY = '还没有群：在群里 @ 机器人一次，那个群就会出现在这里。'
# The confirmation card's states: waiting for the owner's word, what holds once given (here or on the 群聊 card), and
# no record of the group any more. Each with its tag in the title bar.
ASKING, ON, OFF, MISSING = 'asking', 'on', 'off', 'missing'
ASK_TAGS = {ASKING: ('等你确认', 'orange'), ON: ('已启用', 'green'), OFF: ('没有启用', 'neutral'), MISSING: ('没有记录', 'neutral')}
DECIDED = '⚠️ 这个群已经定过了，这次点击没有改动。'  # A click on a confirmation card of a group decided since.
WHO = '群里所有成员 @ 机器人都能开话题、追问、补充、回答问题、点卡片'
WHAT = ('这些话题在你的机器上跑，用你的全部权限和凭据（文件、git、模型密钥、飞书机器人身份）', '用的是你的模型额度',
        '群里的人都看得到卡片、答案和产物')
WHERE = '设置卡「群聊」标签'


def name_of(record, names):
    chat_id = record['chat_id']
    return names.get(chat_id) or '群 …' + chat_id[-6:]


def ordered(groups, names):
    """Groups waiting for the owner's word first, then the active ones, then by name."""
    return sorted(groups, key=lambda g: (not pending(g), not g.get('active'), name_of(g, names)))


def state_words(record):
    """The group's state: what it is and its colour (the card's tag, the web page's pill)."""
    if record.get('external') is True:
        return '外部群 · 只处理你的消息', 'neutral'
    if record.get('active') is True:
        return '已启用', 'green'
    if pending(record):
        return ASK_TAGS[ASKING]
    return ('未确认', 'orange') if record.get('external') is None and not record.get('deactivated_at') else ('已停用', 'neutral')


def state(record):
    """The group's state as a tag (``state_words``)."""
    return tag(*state_words(record))


def free_mode(record):
    """The group's 免@ mode as set ('' off)."""
    mode = record.get('free_talk')
    return mode if mode in FREE_CHOICES and mode else ''


def free_words(record):
    """Who talks without the @ in the group now, '免@ · 只有你' ('' none). Members only in an active group."""
    mode = free_mode(record)
    if not mode:
        return ''
    if mode != 'owner' and not (record.get('active') is True and record.get('external') is False):
        mode = 'owner'
    picked = len(record.get('free_talk_members') or [])
    return '免@ · ' + {'owner': '只有你', 'some': f'你和 {picked} 人', 'all': '所有人'}[mode]


def free_tag(record):
    """The row's 免@ tag (``free_words``), '' for none."""
    words = free_words(record)
    return ' ' + tag(words, 'blue') if words else ''


def free_limit(record, blocked):
    """Why only some choices are open in the group: a line, '' when all are."""
    if blocked:
        return PROXY_BLOCKED
    if record.get('external') is True:
        return EXTERNAL_ONLY
    return '' if record.get('active') is True and record.get('external') is False else INACTIVE_ONLY


KIND_WORDS = {'personal': '个人空间', 'team': '团队空间', 'proxy': '代答空间'}


def kind_tag(kind):
    """The row's space type as a tag ('' when unknown)."""
    return ' ' + tag(KIND_WORDS[kind], 'blue' if kind == 'personal' else 'neutral') if kind in KIND_WORDS else ''


def group_manager_card(token, view, groups, names, top=(), free=None, kinds=None):
    """The whole card for ``view`` (revision, page, notice; ``free``: the group whose 免@ page it shows) over the
    ``groups`` records; ``top``: elements above the body (the 设置 card's tab row: a page of PAGE_SIZE rows leaves
    room for it). ``free``: None when the deployment offers no 免@, else {'blocked': the proxy's chats}. ``kinds``:
    None unless the deployment sets spaces (harnesses.json ``spaces``), else {chat_id: the space type}: each row says it,
    and a personal group (owned by the settings, not switched here) has no 启用 / 停用."""
    def value(kind, **more):
        return {'kind': kind, 'token': token, 'revision': view['revision'], **more}

    if free is not None and view.get('free'):
        record = next((g for g in groups if g['chat_id'] == view['free']), None)
        if record is not None:
            return free_card(value, view, record, names, top, record['chat_id'] in free.get('blocked', ()))
    groups = ordered(groups, names)
    pages = max(1, -(-len(groups) // PAGE_SIZE))
    page = min(max(int(view.get('page') or 0), 0), pages - 1)
    active = sum(1 for g in groups if g.get('active') is True and g.get('external') is False)
    if kinds is not None:
        active = sum(1 for g in groups if kinds.get(g['chat_id']) == 'team' and g.get('active') is True)
    waiting = sum(1 for g in groups if pending(g))
    counts = f'{len(groups)} 个群 · 已启用 {active}' + (f' · 等你确认 {waiting}' if waiting else '')
    head = panel_header('groups', TITLE, counts if groups else '还没有群')
    elements = list(top) + ([ui.receipt(view['notice'])] if view.get('notice') else [])
    if not groups:
        elements.append(md(EMPTY))
    shown = groups[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    for record in shown:
        personal = kinds is not None and kinds.get(record['chat_id']) == 'personal'
        line = md(f"**{literal(name_of(record, names))}**" + (kind_tag(kinds.get(record['chat_id'])) if kinds is not None else '')
                  + ('' if personal else f" {state(record)}") + (free_tag(record) if free is not None and not personal else ''))
        if personal:
            elements.append(line)
            continue
        buttons = [text_button('免@', value(VIEW, chat=record['chat_id']))] if free is not None else []
        if record.get('external') is True:
            elements.append(item(line, *buttons) if buttons else line)
        elif record.get('active') is True:
            elements.append(item(line, *buttons, text_button('停用', value(SWITCH, chat=record['chat_id'], on=False),
                                                             tone='danger')))
        elif pending(record):  # Its confirmation card's two choices; 先不启用 is 停用 (app/group_manager.py switch).
            elements.append(item(line, *buttons,
                                 text_button('启用', value(SWITCH, chat=record['chat_id'], on=True), tone='primary'),
                                 text_button('先不启用', value(SWITCH, chat=record['chat_id'], on=False))))
        else:
            elements.append(item(line, *buttons, text_button('启用', value(SWITCH, chat=record['chat_id'], on=True),
                                                             tone='primary')))
    if pages > 1:
        def turn(label, to, disabled):
            return callback_button(label, value(PAGE, page=to), style='default', width='fill', size='small',
                                   disabled=disabled)
        elements.append(row((note(f'第 {page + 1}/{pages} 页'), 2), (turn('上一页', page - 1, page == 0), 1),
                            (turn('下一页', page + 1, page == pages - 1), 1), align='center'))
    elements.append(note(NOTE))
    return styled_panel(head, elements, TITLE)


def free_card(value, view, record, names, top, blocked):
    """A group's 免@ page: who talks there without the @ (the open choices a button each, the one set tagged 当前),
    with 你和选中的人 its members to pick from (``view['members']``: [{'id', 'name'}] as read, None when they
    could not be), the picked first."""
    chat, mode = record['chat_id'], free_mode(record)
    name = literal(name_of(record, names))
    head = panel_header('groups', TITLE, f'群「{name_of(record, names)}」· 免@')
    limit = free_limit(record, blocked)
    elements = list(top) + ([ui.receipt(view['notice'])] if view.get('notice') else [])
    elements.append(md(f'谁在群「{name}」里说话**不用 @ 机器人**：'))
    for choice, (label, meaning) in FREE_CHOICES.items():
        line = md(f"**{label}**" + (' ' + tag('当前', 'blue') if choice == mode else '') + '\n' + grey(meaning))
        open_ = not blocked or not choice
        if limit and limit != PROXY_BLOCKED and choice in ('some', 'all'):
            open_ = False
        if choice == mode:
            elements.append(line)
        else:
            elements.append(item(line, text_button('选这个', value(FREE, chat=chat, mode=choice), tone='primary',
                                                   disabled=None if open_ else True)))
    if limit:
        elements.append(note(limit))
    if mode == 'some' and not limit:
        elements.extend(member_rows(value, view, record))
    elements.append(item(note(FREE_NOTE), text_button('返回群列表', value(VIEW, chat=''))))
    return styled_panel(head, elements, TITLE)


def member_rows(value, view, record):
    """The 免@ page's members: the picked first, those no longer listed by the end of their open_id."""
    chat = record['chat_id']
    picked = [m for m in record.get('free_talk_members') or [] if isinstance(m, str)]
    read = view.get('members')
    listed = {m['id']: m.get('name') or '' for m in read or [] if isinstance(m, dict) and isinstance(m.get('id'), str)}
    people = [(m, listed.get(m) or '已不在群里 …' + m[-6:], True) for m in picked]
    people += [(m, n or '…' + m[-6:], False) for m, n in listed.items() if m not in picked]
    rows = [md(f'**选中的人**（{len(picked)}）')]
    if read is None:
        rows.append(note(NO_MEMBERS))
    elif not people:
        rows.append(note(ALONE))
    pages = max(1, -(-len(people) // MEMBER_PAGE))
    page = min(max(int(view.get('member_page') or 0), 0), pages - 1)
    for member, label, on in people[page * MEMBER_PAGE:(page + 1) * MEMBER_PAGE]:
        line = md(literal(label) + (' ' + tag('已选', 'blue') if on else ''))
        rows.append(item(line, text_button('取消' if on else '选上', value(MEMBER, chat=chat, member=member, on=not on),
                                           tone='danger' if on else 'primary')))
    if pages > 1:
        def turn(label, to, disabled):
            return callback_button(label, value(PAGE, page=to, members=True), style='default', width='fill',
                                   size='small', disabled=disabled)
        rows.append(row((note(f'第 {page + 1}/{pages} 页'), 2), (turn('上一页', page - 1, page == 0), 1),
                        (turn('下一页', page + 1, page == pages - 1), 1), align='center'))
    return rows


def folded_card():
    """The menu's own 群聊 card folded in place (app/group_manager.py, a second click on the menu's 群聊)."""
    return folded_panel('groups', TITLE, menu_item(TITLE))


def ask_name(view):
    """The name the confirmation card calls its group by: Feishu's, else the end of its chat id."""
    return view.get('name') or '群 …' + view['chat'][-6:]


def ask_card(view):
    """The confirmation card of group ``view['chat']`` (``name`` as Feishu gave it) in ``state`` (ASK_TAGS): ASKING
    says who could use the bot there and what that means, with 启用 and 先不启用; ON and OFF what holds and where to
    change it; ``notice`` (why the click changed nothing, or a refusal) above them."""
    chat, state, name = view['chat'], view.get('state') or ASKING, ask_name(view)
    shown = literal(name)
    elements = [ui.receipt(view['notice'])] if view.get('notice') else []
    if state == ASKING:
        def value(on):
            return {'kind': CONFIRM, 'chat': chat, 'on': on, 'name': view.get('name') or ''}
        elements += [md(f'你在群「**{shown}**」@ 了机器人，现在那里只处理你的消息。'),
                     md(f'**启用后**，{WHO}：\n' + '\n'.join(f'- {line}' for line in WHAT)),
                     item(md(' '), text_button('启用', value(True), tone='primary'), text_button('先不启用', value(False))),
                     note(f'只在信得过的群里启用。先不启用的话，你再 @ 也不会再问；要改，在{WHERE}里改。')]
    elif state == ON:
        elements += [md(f'群「**{shown}**」已启用：{WHO}。'),
                     note(f'{WHAT[0]}，{WHAT[1]}。要停用，在{WHERE}里点「停用」。')]
    elif state == OFF:
        elements += [md(f'群「**{shown}**」没有启用：那里只处理你的消息，你再 @ 也不会再问。'),
                     note(f'要启用，在{WHERE}里点「启用」。')]
    else:
        elements.append(md('没有这个群的记录了，这次点击什么都没改。'))
    head = panel_header('groups', TITLE, f'群「{name}」', [ASK_TAGS[state]])
    return styled_panel(head, elements, ask_summary(view))


def ask_summary(view):
    """The confirmation card's line in the chat list."""
    state, name = view.get('state') or ASKING, ask_name(view)
    return {ASKING: f'要你定：群「{name}」启不启用', ON: f'群「{name}」已启用', OFF: f'群「{name}」没有启用'}.get(state, TITLE)


def ask_text(view):
    """The confirmation card as Markdown, for when Feishu takes no card: the same words, the 群聊 tab for its buttons."""
    state, name = view.get('state') or ASKING, ask_name(view)
    lines = [view['notice']] if view.get('notice') else []
    if state == ASKING:
        lines += [f'**{ask_summary(view)}**', f'你在群「{name}」@ 了机器人，现在那里只处理你的消息。',
                  f'启用后，{WHO}。' + '；'.join(WHAT) + '。',
                  f'要启用，在{WHERE}里点「启用」；不点就一直只处理你的消息。']
    elif state == ON:
        lines += [f'群「{name}」已启用：{WHO}。要停用，在{WHERE}里点「停用」。']
    elif state == OFF:
        lines += [f'群「{name}」没有启用：那里只处理你的消息。要启用，在{WHERE}里点「启用」。']
    else:
        lines.append('没有这个群的记录了，这次点击什么都没改。')
    return '\n'.join(lines)
