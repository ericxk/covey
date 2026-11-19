"""The 设置 card's 脱敏 tab (app/redaction_panel.py), one layer: a small stats line, 填词 (a box a term a line, 加入词表),
a search box, the list a term a line each with its 删除, then folds for 开关与上游地址, 试一下 and 记录 (closed), and
last the web dashboard's link when the deployment serves one (its 换口令 is cards/redaction_link.py ROTATE_KIND).

No term is on the card (docs/usage.md 敏感词脱敏): a row names a term by core/redaction.py ``masked`` and by a key that
tells nothing of it, a stand-in is never shown (RX and 8 hex digits of the term's hash, next to its first character and
length, would give a short term away; the owner no longer names one, 2026-11-14), and what the owner typed into a box
is never its default value. Each button's value carries its kind, the card's token and the revision drawn; a form's
submit button carries one too (as cards/proxy_panel.py's 查找). PAGE and EDIT, the tab row and the row editor of the
2026-11-13 layout, stay in KINDS so a card sent then still answers."""
from __future__ import annotations

from covey.cards import ui
from covey.core.text import literal
from covey.cards.elements import (callback_button, collapsible, grey, item, md, note, panel_header, plain, styled_panel,
                                  tag, text_button)
from covey.cards.redaction_link import ROTATE_KIND

TITLE = '脱敏看板'
SUBTITLE = '填入敏感词，替身词自动生成'
# One layer (the owner's decision of 2026-11-14): the stats line, 填词, the list, three folds (开关, 试一下, 记录), the web link.
PAGE = 'bridge_redaction_page'  # Retired: the tab row of the four pages (a card sent 2026-11-13 still has it).
LIST = 'bridge_redaction_list'  # The list's page (``at``); ``clear`` ends a search.
FIND = 'bridge_redaction_find'  # 查找: the form's QUERY.
ADD = 'bridge_redaction_add'  # 加入词表: the form's LINES.
TERM = 'bridge_redaction_term'  # A row's 删除 (``op`` delete), by its ``key``; ``op`` edit is retired.
EDIT = 'bridge_redaction_edit'  # Retired: the row editor's buttons (stand-in, match, done).
UNDO = 'bridge_redaction_undo'  # 撤销 the card's last 删除.
SWITCH = 'bridge_redaction_switch'  # ``name`` (a SWITCH_ROWS key) set to ``on``.
UPSTREAM = 'bridge_redaction_upstream'  # ``op`` save (the form's UPSTREAM_FIELD) or clear.
PROBE = 'bridge_redaction_probe'  # 探测.
PREVIEW = 'bridge_redaction_preview'  # 试一下: the form's TEXT.
OPEN = 'bridge_redaction_open'  # 试一下 or 修改地址 (``name`` try, upstream) opened or closed (``on``): a form cannot sit in a fold.
RETIRED = frozenset({PAGE, EDIT})  # Their click draws the card again with RETIRED_WORDS.
KINDS = frozenset({PAGE, LIST, OPEN, FIND, ADD, TERM, EDIT, UNDO, SWITCH, UPSTREAM, PROBE, PREVIEW})
QUERY, LINES, UPSTREAM_FIELD, TEXT = 'query', 'lines', 'upstream', 'text'
INPUT_LIMIT = 1000  # The most a Feishu input takes.
PAGE_SIZE = 10  # A row is one line.
RECENT = 10  # Turns the 记录 fold lists.
TOPICS = 10  # Topics it lists.
# 新话题默认开 moved to the 新话题 page's 脱敏 row (the owner, 2026-11-19, T546; core/migrate.py redaction-default).
SWITCH_ROWS = {'ignore_case': ('不区分大小写', '其他大小写写法各有自己的自动替身，还原成原样写法'),
               'restore': ('回复还原', '关掉后模型回复里的替身词原样留着')}
FOLDS = {'switches': '开关与上游地址', 'records': '记录'}  # element_id -> title: collapsible_panel can hold no form (the client
#   refuses the card: 10002), so 试一下 and the upstream address's box open by a click and a redraw (OPEN).
EMPTY = '词表是空的：在上面的框里填词。'
IDLE = '⚠️ 词表是空的：开着脱敏的 {} 个话题现在什么都不替换。'
NOT_FOUND = '没有找到。'
ADD_HINT = '每行一个词，一次最多 1000 字；替身词自动生成。'
RETIRED_WORDS = 'ℹ️ 这个按钮已更新：卡片按新布局重画了（替身词现在自动生成，不再手动指定）。'
TRY_HINT = '用已保存的词表和开关替换一段文字。卡片上只显示替换后的样子，不显示原文和还原结果。'
NOTE = '改动从下一轮起生效。卡上的词只显示首字和字数，框里输入的内容不留在卡片上。'
WEB_NOTE = '网页看板（可选）能看到完整原词，要经 SSH 隧道或公司内网打开。'
ROTATED = '口令已换：之前的链接和已登录的浏览器都失效了，点“打开网页看板”用新口令登录。'


def submit(label, value, name, *, primary=False):
    """A form's submit button named ``name`` that also calls back with ``value``."""
    button = callback_button(label, value, style='primary_filled' if primary else 'default', size='small')
    button.update(name=name, form_action_type='submit')
    return button


def box(name, placeholder, *, default='', lines=1, limit=INPUT_LIMIT):
    """A form's input: one line, or ``lines`` rows growing to twice as many."""
    element = {'tag': 'input', 'name': name, 'width': 'fill', 'max_length': limit, 'placeholder': plain(placeholder)}
    if lines > 1:
        element.update(input_type='multiline_text', rows=lines, auto_resize=True, max_rows=lines * 2)
    if default:
        element['default_value'] = default
    return element


def form(name, *elements):
    return {'tag': 'form', 'name': name, 'elements': list(elements)}


def redaction_card(token, view, data, *, top=(), web=None):
    """The whole tab for ``view`` (revision, at, found, notice, undo, preview, probing, probe, rotated) over ``data``
    (app/redaction_panel.py ``facts``: error, config without its terms, rows, count, week, topics, turns); ``top``: the
    设置 card's tab row; ``web``: the web dashboard's part (link, notes, listening, or error), None when the
    deployment serves none. Older views' ``page`` and ``edit`` are ignored."""
    def value(kind, **more):
        return {'kind': kind, 'token': token, 'revision': view['revision'], **more}

    elements = [*top, note(f"{data['count']} 个词 · 近 7 天替换 {data['week']} 次 · {len(data['topics'])} 个话题开着")]
    if view.get('notice'):
        elements.append(item(ui.receipt(view['notice']), text_button('撤销', value(UNDO), tone='primary'))
                        if view.get('undo') else ui.receipt(view['notice']))
    if data['error']:
        elements.append(md('⚠️ ' + data['error']))  # app/redaction_panel.py UNREADABLE: it names no term.
    else:
        if not data['count'] and data['topics']:
            elements.append(md(IDLE.format(len(data['topics']))))
        elements += terms_part(value, view, data)
        opened = view.get('open') if isinstance(view.get('open'), list) else []
        elements += [collapsible('rx_switches', FOLDS['switches'], switches_part(value, view, data['config'], opened))]
        if 'upstream' in opened:
            elements += upstream_form(value, data['config'])
        elements += try_part(value, view, 'try' in opened)
        elements.append(collapsible('rx_records', FOLDS['records'], records_part(data)))
    elements.append(note(NOTE))
    if web is not None:
        elements += web_part(token, view, web)
    return styled_panel(panel_header('redaction', TITLE, SUBTITLE), elements, TITLE)


def terms_part(value, view, data):
    """填词 (a box a term a line, 加入词表), the search box (always there; what it found under it) and a page of rows,
    newest first."""
    rows, found = data['rows'], view.get('found')
    parts = [form('redaction_add', box(LINES, '每行一个词', lines=3), submit('加入词表', value(ADD), 'add', primary=True)),
             note(ADD_HINT),
             form('redaction_find', item(box(QUERY, '搜索词表里的词'), submit('查找', value(FIND), 'find')))]
    if found is not None:
        keys = set(found)
        rows = [r for r in rows if r['key'] in keys]
        parts.append(item(md(f'**找到 {len(rows)} 个**' if rows else NOT_FOUND),
                          text_button('清除搜索', value(LIST, at=0, clear=True), tone='primary')))
    pages = max(1, -(-len(rows) // PAGE_SIZE))
    at = min(max(view.get('at') if isinstance(view.get('at'), int) else 0, 0), pages - 1)
    for r in rows[at * PAGE_SIZE:(at + 1) * PAGE_SIZE]:
        parts.append(item(term_line(r), text_button('删除', value(TERM, key=r['key'], op='delete'), tone='danger')))
    if not data['count']:
        parts.append(md(EMPTY))
    if pages > 1:
        def turn(label, to, disabled):
            return callback_button(label, value(LIST, at=to), style='default', width='fill', size='small',
                                   disabled=disabled)
        parts.append({'tag': 'column_set', 'flex_mode': 'stretch', 'horizontal_spacing': '8px', 'columns': [
            {'tag': 'column', 'width': 'weighted', 'weight': weight, 'vertical_align': 'center', 'elements': [element]}
            for element, weight in ((note(f'第 {at + 1}/{pages} 页'), 2), (turn('上一页', at - 1, at == 0), 1),
                                    (turn('下一页', at + 1, at == pages - 1), 1))]})
    return parts


def term_line(r):
    """A row's one line: the masked term, its hits in the last 7 days, and a tag only where something is out of the
    ordinary (a stand-in an older version set, a stand-in shared with another term, replies not turned back)."""
    tags = [tag('自定义替身')] if r['stand_in'] or r['anywhere'] else []
    tags += [tag(r['restore'])] if r['restore'] in ('共用·不还原', '不还原') else []
    return md(f"**{literal(r['label'])}** " + ' '.join(tags) + (' ' if tags else '') + grey(f"近 7 天 {r['week']} 次"))


def switches_part(value, view, config, opened=()):
    """The three switches, a row each, then the upstream address (修改地址 opens its box under the folds) and 探测."""
    parts = []
    for name, (label, hint) in SWITCH_ROWS.items():
        on = bool(config[name])
        line = md(f"**{label}** {tag('开', 'green') if on else tag('关')}\n" + grey(hint))
        parts.append(item(line, text_button('关掉' if on else '打开', value(SWITCH, name=name, on=not on),
                                            tone='' if on else 'primary')))
    upstream = config['upstream']
    parts.append(item(md('**上游地址** ' + (literal(upstream) if upstream else grey('没设：每轮自动探测（Claude 账号用）'))),
                      text_button('收起' if 'upstream' in opened else '修改地址',
                                  value(OPEN, name='upstream', on='upstream' not in opened))))
    buttons = [text_button('探测', value(PROBE), tone='primary', disabled=True if view.get('probing') else None)]
    if upstream:
        buttons.append(text_button('清空', value(UPSTREAM, op='clear')))
    probe = view.get('probe') or {}
    if view.get('probing'):
        said = '正在探测…'
    elif probe.get('error'):
        said = '⚠️ 没探测到：' + literal(probe['error'])
    elif probe.get('upstream'):
        said = f"探测结果：{literal(probe['upstream'])}（{literal(probe.get('source', ''))}）"
    else:
        said = '探测：没设上游地址时，看这一轮的请求会发到哪里（只看，不改设置）。'
    parts.append(item(note(said), *buttons))
    return parts


def upstream_form(value, config):
    """The upstream address's box (open by 修改地址), under the folds."""
    typed = box(UPSTREAM_FIELD, 'https://…（留空：每轮自动探测）', default=config['upstream'], limit=500)
    return [md('**上游地址**'), form('redaction_upstream', item(typed, submit('保存', value(UPSTREAM, op='save'),
                                                                          'save_upstream')))]


def try_part(value, view, opened):
    """试一下: a button that opens the box (a form, so not a fold) and what the text would go out as."""
    parts = [item(md('**试一下**'), text_button('收起' if opened else '展开', value(OPEN, name='try', on=not opened)))]
    if not opened:
        return parts
    parts += [note(TRY_HINT), form('redaction_try', box(TEXT, '粘贴一段文字', lines=4),
                                   submit('试一下', value(PREVIEW), 'try', primary=True))]
    result = view.get('preview')
    if isinstance(result, dict):
        shown = '\n'.join(literal(line) for line in str(result.get('redacted', '')).splitlines()) or grey('（空）')
        if not result.get('restore'):
            back = '回复还原关着：模型回复里的替身词不会换回原词'
        else:
            back = '还原后与原文一致 ✓' if result.get('same') else '⚠️ 还原后与原文不一致'
        parts += [md('**发出去的样子**'), md(shown),
                  note(f"替换了 {result.get('count', 0)} 处（{result.get('terms', 0)} 个词）；{back}")]
    return parts


def records_part(data):
    """The topics with redaction on among those the recorded turns name, and the latest turns."""
    parts = [md('**开着脱敏的话题**')]
    if not data['topics']:
        parts.append(note('还没有开着脱敏的话题跑过。'))
    for topic in data['topics'][:TOPICS]:
        name = (f"T{topic['number']} " if topic.get('number') else '') + literal(topic.get('title') or '（没有标题）')
        parts.append(md(f"{name}\n" + grey(f"{topic['turns']} 轮 · 替换 {topic['hits']} 处 · 最近 {topic['last']}")))
    parts.append(md(f'**最近 {RECENT} 轮**'))
    if not data['turns']:
        parts.append(note('还没有记录。'))
    for turn in data['turns'][:RECENT]:
        where = f"T{turn['number']}" if turn.get('number') else '—'
        parts.append(md(grey(f"{turn['time']} · {where} · {literal(turn['model'] or '—')} · 请求 {turn['requests']} 次"
                             f" · 替换 {turn['replaced']} 处")))
    parts.append(note('只列最近记录的 500 轮里的话题和轮次。'))
    return parts


def web_part(token, view, web):
    """The web dashboard, the card's last line: a note with its link (it carries the dashboard's token) and 换口令, or
    what is wrong with it."""
    if web.get('error'):
        return [md('**网页看板** ' + tag('设置不对', 'red')), note(literal(web['error']))]
    open_page = {'tag': 'button', 'text': plain('打开网页看板'), 'type': 'default', 'size': 'small',
                 'behaviors': [{'type': 'open_url', 'default_url': web['link']}]}
    rotate = callback_button('换口令', {'kind': ROTATE_KIND, 'token': token}, style='default', size='small')
    down = ' ' + tag('没有在监听', 'red') if not web['listening'] else ''
    parts = [md(ROTATED)] if view.get('rotated') else []
    return parts + [item(note(' '.join([WEB_NOTE, *web['notes']])) if not down else md(down + ' ' + grey(WEB_NOTE)),
                         open_page, rotate)]
