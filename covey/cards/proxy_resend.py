"""The 重新发送 card (app/proxy_resend.py): in a contact's 代答 topic, a reply that never went out
(Enter never pressed, every try), with one button that sends the saved reply again, and then what
came of it. The button's value carries its kind, the batch and a digest of the reply it shows.

The same card says a turn would not answer for the owner ([不回复], ``declined``) when the model suggested
a reply: its 按建议回复 sends that suggestion as written, and the card then goes on as above (repo review U5)."""
from __future__ import annotations

from covey.core.text import literal
from covey.cards.elements import markdown_panel, panel_card
from covey.core.proxy import digest, unsent

RESEND = 'bridge_proxy_resend'
SUGGEST = 'bridge_proxy_suggest'  # 按建议回复: the first send of a declined turn's suggestion.
SHOWN = 1500  # Characters of the reply the card shows.
# What the card says, by the state of the batch's send: (heading, whether the button is there).
STATES = {
    'declined': ('⏸ 没有代你回复', True),
    'sending': ('⏳ 正在发送…', True),  # After 按建议回复: its button drawn off.
    'failed': ('⚠️ 代答没有发出', True),
    'again': ('⚠️ 还是没发出', True),
    'resending': ('⏳ 正在重新发送…', True),  # Its button drawn off.
    'sent': ('✅ 已发出', False),
    'uncertain': ('⚠️ 可能已发出，没能确认', False),
    'void': ('对方有新消息，这条已作废', False),
    'dropped': ('这条不再发', False),
}
LOGIN_HINT = '网页端登录失效的话，先点机器人菜单「代答」，在代答卡上点“网页端登录”。'


def reply_digest(reply):
    return digest(str(reply or ''))[:12]


def resend_card(view, batch, who, chat=''):
    """The whole card for ``view`` ({state, notice}) of ``batch``; ``who`` the person the reply is for,
    ``chat`` the title of the chat it goes to (``who`` when not given). Why it did not go out is in
    words, what the page compared (positions) in a folded 详情."""
    state = view.get('state') if view.get('state') in STATES else 'failed'
    heading, button = STATES[state]
    reply, reason = str(batch.get('reply') or ''), str(batch.get('reason') or '')
    words, details = unsent(reason, chat or who, who, again=state == 'again')
    lines = {
        'declined': (f'原因和建议见上面 #{batch.get("reply_job_id")} 的回答。要回的话，点“按建议回复”，以你的身份原样发出'
                     '下面这句（先核对会话）；也可以在本话题 @机器人 告诉我怎么回。'),
        'sending': '下一次轮询（30 秒内）开始以你的身份发，网页端先核对会话，约 1 分钟。',
        'failed': f'没发出：{words}。',
        'again': f'还是没发出：{words}。',
        'resending': '下一次轮询（30 秒内）开始以你的身份发，网页端先核对会话，约 1 分钟。',
        'sent': f'已以你的身份发给{who}，读回确认。',
        'uncertain': f'按下了发送，但读回时没找到（{reason or "没有结果"}）。不会自动重发，请到和{who}的会话里看一眼。',
        'void': f'{who}又发来了新消息，这条回复不再发，新消息另外处理。',
        'dropped': f'{reason or "这条代答已结束"}，不再发。',
    }
    shown = reply if len(reply) <= SHOWN else reply[:SHOWN] + f'…（共 {len(reply)} 字）'
    elements = [{'tag': 'markdown', 'content': f'**{heading}**\n{lines[state]}'}]
    if details and state in ('failed', 'again'):
        panel = markdown_panel('unsent_details', '详情', '最后一次核对：\n' + '\n'.join(
            '- ' + literal(line[2:]) if line.startswith('- ') else literal(line) for line in details.split('\n')),
            size='notation', quiet=True)
        elements.append(panel)
    label = {'sent': '发出的原文：', 'declined': '建议回复：'}.get(state, '要发的原文：')
    elements += [
        {'tag': 'markdown', 'content': label + '\n' + '\n'.join(literal(line) for line in shown.split('\n'))},
    ]
    if button:
        suggest = state in ('declined', 'sending')
        elements.append({
            'tag': 'button', 'text': {'tag': 'plain_text', 'content': '按建议回复' if suggest else '重新发送'},
            'type': 'primary_filled', 'width': 'default', 'disabled': state in ('resending', 'sending'),
            'behaviors': [{'type': 'callback', 'value': {'kind': SUGGEST if suggest else RESEND, 'batch': batch['id'],
                                                         'reply': reply_digest(reply)}}]})
    if state in ('failed', 'again'):
        elements.append({'tag': 'markdown', 'text_size': 'notation', 'content': (
            '点“重新发送”原样再发这条，不重新生成。也可以在本话题 @机器人 说“重新回复”，或直接说怎么回，那一轮的回复会发给对方。'
            + (LOGIN_HINT if '登录' in reason else ''))})
    if view.get('notice'):
        elements.append({'tag': 'markdown', 'content': view['notice']})
    return panel_card(elements, heading)
