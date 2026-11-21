"""代答's check of each reply before it goes out (app/proxy.py check_reply, core/proxy.py reply_hits and
COVEY_PROXY_REPLY_CHECK: count only by default) and the 代答 card's 最近 (core/proxy.py recent, cards/proxy_panel.py)."""
import json
import os
import time
import unittest
from unittest import mock

from support import require_extension
from support.proxy import PERSON, ProxyCase
from covey.cards import proxy_panel as panel
from covey.core import proxy as records

require_extension()  # ProxyCase runs the company's sender (core/extensions.py hook proxy_sender).

SENT = {'ok': True, 'submitted': True}
# Obviously fake values in the shapes the check looks for, made up here so no real-looking one is in the source.
OPEN_ID, CHAT_ID = 'ou_' + '0123456789abcdef' * 2, 'oc_' + 'fedcba9876543210' * 2
KEY, GITHUB = 'sk-' + 'ant-api03-' + 'x' * 30, 'ghp' + '_' + 'a' * 36


class ReplyHitsTests(unittest.TestCase):
    def test_each_kind_and_what_is_left_alone(self):
        hits = {
            '文件在 /mnt/data/x/y.py 里': ['path'], '放在 ~/work/notes.md 了': ['path'], r'C:\Users\me\a.txt': ['path'],
            f'问 {OPEN_ID} 吧': ['id'], f'群 {CHAT_ID}': ['id'],
            '6f1c2d3e-1234-4abc-8def-0123456789ab': ['id'], f'key 是 {KEY}': ['secret'],
            'token=abcdef123456': ['secret'], GITHUB: ['secret'],
            '我们【时间】见': ['placeholder'], '你好 {{name}}': ['placeholder'], 'xx月xx日开会': ['placeholder'],
            'TODO 再说': ['placeholder'], f'路径 /home/me/k 和 {CHAT_ID[:20]} 和【某】': ['path', 'id', 'placeholder'],
        }
        for text, kinds in hits.items():
            self.assertEqual(records.reply_hits(text), kinds, text)
        for text in ('好的，我看下', '见 https://example.com/home/user', '日期 2026/11/21', '上班/下班/加班都行', '接口是 /api/v1/users',
                     'commit a1b2c3d 修了', 'Todo list 不算', '在 harness_bridge/app 下', ''):
            self.assertEqual(records.reply_hits(text), [], text)

    def test_the_switch_counts_unless_it_says_hold_or_off(self):
        for value, mode in (('', records.CHECK_COUNT), ('hold', records.CHECK_HOLD), ('HOLD', records.CHECK_HOLD),
                            ('off', records.CHECK_OFF), ('0', records.CHECK_OFF), ('maybe', records.CHECK_COUNT)):
            with mock.patch.dict(os.environ, {records.CHECK_ENV: value}):
                self.assertEqual(records.reply_check_mode(), mode, value)


class ReplyCheckTests(ProxyCase):
    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.dict(os.environ, {}, clear=False))
        for name in (records.CHECK_ENV, 'LARK_BRIDGE_PROXY_REPLY_CHECK'):
            os.environ.pop(name, None)

    def counts(self):
        path = records.proxy_dir(self.state) / records.CHECKS_FILE
        return json.loads(path.read_text()) if path.exists() else None

    def test_by_default_a_hit_is_counted_and_logged_and_the_reply_goes_out_as_before(self):
        batch = self.submitted()
        self.run_tick('在 /mnt/data/docs/api.md 里', SENT).assert_called_once()
        self.assertEqual(records.read_batch(self.state, batch['id'])['status'], 'sent')
        counts = self.counts()
        self.assertEqual((counts['checked'], counts['hit'], counts['kinds']), (1, 1, {'path': 1}))
        [hit] = counts['recent']
        self.assertEqual((hit['batch'], hit['kinds'], hit['mode']), (batch['id'][:8], ['path'], 'count'))
        [line] = [line for line in self.logs if line.startswith('proxy reply check')]
        self.assertIn('hits=path mode=count; sent as before', line)
        self.assertNotIn('/mnt/data', line)  # The log says the kinds, never the reply.

    def test_a_reply_decided_again_is_counted_and_said_once(self):
        self.assertTrue(records.count_check(self.state, ['path'], batch='b' * 32, job_id=4))
        self.assertFalse(records.count_check(self.state, ['path'], batch='b' * 32, job_id=4))  # The same reply.
        self.assertTrue(records.count_check(self.state, [], batch='b' * 32, job_id=5))  # The owner's next turn.
        counts = self.counts()
        self.assertEqual((counts['checked'], counts['hit'], counts['kinds'], len(counts['recent'])), (2, 1, {'path': 1}, 1))

    def test_a_clean_reply_is_counted_with_nothing_logged(self):
        self.submitted()
        self.run_tick('在共享盘的 docs 里', SENT).assert_called_once()
        counts = self.counts()
        self.assertEqual((counts['checked'], counts['hit'], counts['recent']), (1, 0, []))
        self.assertFalse([line for line in self.logs if line.startswith('proxy reply check')])

    def test_held_on_a_hit_the_reply_waits_for_the_owner_on_its_card(self):
        os.environ[records.CHECK_ENV] = 'hold'
        batch = self.submitted()
        reply = f'问 {OPEN_ID} 吧，【时间】再约'
        self.run_tick(reply, SENT).assert_not_called()
        held = records.read_batch(self.state, batch['id'])
        self.assertEqual((held['status'], held['reply']), ('held', reply))
        self.assertEqual(held['reason'], '回复里有内部编号、没填的占位符，先给你看')
        self.assertTrue(held.get('resend_card'))  # 按建议回复 sends it as it is; the owner looked at it.
        self.assertEqual(self.counts()['recent'][0]['mode'], 'hold')
        # The owner's turn in the topic is checked too: a held batch stays held with that turn's reply.
        self.owner_turn(held)
        with mock.patch.object(self.proxy, 'reply_of', return_value='TODO 回头说'), \
                mock.patch.object(self.proxy.web, 'web_send', return_value=SENT) as sent, \
                mock.patch.object(self.proxy.web_login, 'check_login'):
            self.proxy.tick(self.ctx, self.settings())
        sent.assert_not_called()
        again = records.read_batch(self.state, batch['id'])
        self.assertEqual((again['status'], again['reply'], again['reason']),
                         ('held', 'TODO 回头说', '回复里有没填的占位符，先给你看'))

    def test_off_checks_nothing(self):
        os.environ[records.CHECK_ENV] = 'off'
        self.submitted()
        self.run_tick('在 /mnt/data/docs/api.md 里', SENT).assert_called_once()
        self.assertIsNone(self.counts())


class RecentTests(ProxyCase):
    def batch(self, mid, status, ago, **fields):
        made = records.new_batch(PERSON, [records.incoming({'message_id': mid, 'content': '在吗', 'sender': {}})])
        at = round(time.time() - ago)
        records.write_batch(self.state, {**made, 'status': status, 'history': [[status, at]], **fields})

    def test_three_kinds_of_the_last_day_newest_first(self):
        self.batch('m1', 'held', 600, reply='明天给你')  # Drafted, not sent.
        self.batch('m2', 'uncertain', 300, reply='好的')  # May have gone out.
        self.batch('m3', 'skipped', 60, reason=records.OWNER_REPLIED)  # The owner answered himself.
        self.batch('m4', 'held', 30)  # Held without a reply: nothing drafted.
        self.batch('m5', 'skipped', 20, reason='不用回：对方只是收尾')
        self.batch('m6', 'sent', 10, reply='在的')
        self.batch('m7', 'held', 2 * 86400, reply='很早的')  # Older than a day.
        found = records.recent(self.state)
        self.assertEqual([row['kind'] for row in found], [records.ANSWERED, records.UNSURE, records.DRAFTED])
        self.assertEqual({row['contact'] for row in found}, {PERSON['key']})
        for n in range(8):
            self.batch(f'n{n}', 'uncertain', 100 + n)
        self.assertEqual(len(records.recent(self.state)), records.RECENT_ROWS)

    def test_the_card_lists_them_by_name_and_has_no_section_without_them(self):
        rows = [{'kind': records.DRAFTED, 'contact': PERSON['key'], 'at': time.time() - 60},
                {'kind': records.UNSURE, 'contact': 'p2p:ou_gone', 'at': time.time() - 2 * 86400}]
        part = panel.recent_part(rows, [PERSON])
        text = json.dumps(part, ensure_ascii=False)
        self.assertIn('最近', text)
        self.assertIn("**张三** <text_tag color='neutral'>拟好没发</text_tag>", text)
        self.assertIn('**已移出的联系人** <text_tag color=\'neutral\'>发送结果待核</text_tag>', text)
        self.assertRegex(text, r'\d\d-\d\d \d\d:\d\d')  # Not today: with the date.
        self.assertEqual(panel.recent_part([], [PERSON]), [])
        card = self.proxy.panel.card(self.ctx, 'tok', {'revision': 1, 'token': 'tok'})
        self.assertNotIn('拟好没发', json.dumps(card, ensure_ascii=False))
        self.batch('m1', 'held', 60, reply='明天给你')
        card = self.proxy.panel.card(self.ctx, 'tok', {'revision': 1, 'token': 'tok'})
        self.assertIn('拟好没发', json.dumps(card, ensure_ascii=False))


if __name__ == '__main__':
    unittest.main()
