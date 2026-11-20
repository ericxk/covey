"""The bot's events over the SDK's WebSocket (feishu/ws_intake.py, COVEY_EVENT_TRANSPORT=sdk).

Envelopes below are Feishu open-platform v2 events with made-up values. The converters must give the dict lark-cli's
``--jq`` of the key prints; no connection is opened and nothing reaches Feishu."""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from covey.feishu import events, ws_intake
from covey.feishu.events import (CARD_ACTION_EVENT_KEY, MENU_EVENT_KEY, MESSAGE_EVENT_KEY, READ_EVENT_KEY, REPLY_CONTEXT)

ROOT = Path(__file__).resolve().parents[1]
HEADER = {'event_id': 'ev_1', 'token': 'fake', 'create_time': '1790000000123', 'tenant_key': 't1',
          'app_id': 'cli_fake'}


def envelope(event_type, event, **header):
    return {'schema': '2.0', 'header': {**HEADER, **header, 'event_type': event_type}, 'event': event}


def message(content, message_type='text', **fields):
    return envelope(MESSAGE_EVENT_KEY, {
        'sender': {'sender_id': {'open_id': 'ou_owner', 'union_id': 'on_1', 'user_id': 'u1'}, 'sender_type': 'user',
                   'tenant_key': 't1'},
        'message': {'message_id': 'om_1', 'chat_id': 'oc_1', 'chat_type': 'p2p', 'message_type': message_type,
                    'content': json.dumps(content, ensure_ascii=False), 'create_time': '1790000000000',
                    'update_time': '1790000000000', 'mentions': [], **fields}})


CARD = envelope(CARD_ACTION_EVENT_KEY, {
    'operator': {'tenant_key': 't1', 'user_id': 'u1', 'open_id': 'ou_owner', 'union_id': 'on_1'}, 'token': 'c-fake',
    'action': {'value': {'kind': 'stop', 'job': 7}, 'tag': 'button', 'name': 'stop_btn'}, 'host': 'im_message',
    'context': {'open_message_id': 'om_card', 'open_chat_id': 'oc_1'}})
MENU = envelope(MENU_EVENT_KEY, {'event_key': 'covey.board', 'timestamp': 1790000000,
                                 'operator': {'operator_name': 'Owner',
                                              'operator_id': {'open_id': 'ou_owner', 'union_id': 'on_1', 'user_id': 'u1'}}})
READ = envelope(READ_EVENT_KEY, {'reader': {'reader_id': {'open_id': 'ou_owner'}, 'read_time': '1790000000500',
                                            'tenant_key': 't1'}, 'message_id_list': ['om_a', 'om_b']})


def frame(raw):
    return json.dumps(raw, ensure_ascii=False).encode()


class ConverterTest(unittest.TestCase):
    def test_message_has_the_fields_of_its_jq(self):
        event = ws_intake.message_event(message({'text': '你好 @_user_1'}, mentions=[
            {'key': '@_user_1', 'id': {'open_id': 'ou_bot', 'union_id': 'on_b', 'user_id': ''}, 'name': 'Covey',
             'tenant_key': 't1'}], root_id='om_root', parent_id='om_parent', thread_id='omt_1'))
        names = events.MESSAGE_EVENT_JQ.strip('{}').replace(' ', '').split(',')
        self.assertEqual(set(event), set(names))
        self.assertEqual(event['content'], '你好 @Covey')
        self.assertEqual((event['message_id'], event['id'], event['chat_id'], event['chat_type']),
                         ('om_1', 'om_1', 'oc_1', 'p2p'))
        self.assertEqual((event['sender_id'], event['sender_type']), ('ou_owner', 'user'))
        self.assertEqual((event['root_id'], event['reply_to'], event['thread_id']), ('om_root', 'om_parent', 'omt_1'))
        self.assertIsNone(event['parent_id'])  # lark-cli names the parent reply_to
        self.assertEqual(event['mentions'], [{'id': 'ou_bot', 'key': '@_user_1', 'name': 'Covey'}])
        self.assertIsNone(ws_intake.message_event(message({'text': 'x'}))['mentions'])  # lark-cli: null, not []
        self.assertEqual((event['event_id'], event['timestamp'], event['create_time'], event['type']),
                         ('ev_1', '1790000000123', '1790000000000', MESSAGE_EVENT_KEY))
        self.assertIsNone(event['update_time'])  # only when it differs from create_time

    def test_message_update_time_and_bot_sender(self):
        raw = message({'text': 'x'})
        raw['event']['message']['update_time'] = '1790000009000'
        raw['event']['sender']['sender_type'] = 'app'
        event = ws_intake.message_event(raw)
        self.assertEqual((event['update_time'], event['sender_type']), ('1790000009000', 'bot'))

    def test_other_types_keep_the_message_api_json(self):
        image = {'image_key': 'img_v3_02abc_1234-aaaa'}
        event = ws_intake.message_event(message(image, 'image'))
        self.assertEqual(json.loads(event['content']), image)
        self.assertEqual(event['message_type'], 'image')
        from covey.feishu.message_parts import collect_message_parts
        self.assertEqual(collect_message_parts(event['content'])[1], ['img_v3_02abc_1234-aaaa'])

    def test_a_post_reads_as_lark_cli_prints_it(self):
        """Posts sent and read back on the test bot, 2026-11-20: lark-cli's +messages-mget of each, byte for byte.
        Elements of a paragraph run together (message_parts.py would put "1. " and "第一项" on two lines)."""
        cases = [
            ({'title': 'T575 标题', 'content': [
                [{'tag': 'text', 'text': '普通 ', 'style': []}, {'tag': 'text', 'text': '加粗', 'style': ['bold']},
                 {'tag': 'text', 'text': ' 结尾', 'style': []}],
                [{'tag': 'a', 'href': 'https://example.com/x', 'text': '链接文字', 'style': []},
                 {'tag': 'text', 'text': ' 后面', 'style': []}],
                [{'tag': 'at', 'user_id': '@_user_1', 'user_name': '主人', 'style': []},
                 {'tag': 'text', 'text': ' 请看', 'style': []}],
                [{'tag': 'code_block', 'language': 'PYTHON', 'text': 'print(1)\n\n\n\n'}], [{'tag': 'hr'}],
                [{'tag': 'emotion', 'emoji_type': 'SMILE'}, {'tag': 'text', 'text': ' 表情', 'style': []}]]},
             'T575 标题\n普通 **加粗** 结尾\n[链接文字](https://example.com/x) 后面\n@主人 请看\n\n```PYTHON\n'
             'print(1)\n\n\n\n\n```\n\n\n---\n\n:SMILE: 表情'),
            ({'title': '', 'content': [
                [{'tag': 'text', 'text': '斜', 'style': ['italic']}, {'tag': 'text', 'text': '下划', 'style': ['underline']},
                 {'tag': 'text', 'text': '删', 'style': ['lineThrough']},
                 {'tag': 'text', 'text': '粗斜', 'style': ['bold', 'italic']}],
                [{'tag': 'text', 'text': '行内 `code` 和 * 星号 _ 下划线'}], [{'tag': 'a', 'text': '', 'href': 'https://example.com/e'}]]},
             '*斜*<u>下划</u>~~删~~***粗斜***\n行内 `code` 和 * 星号 _ 下划线\nhttps://example.com/e'),
            ({'title': '', 'content': [[{'tag': 'text', 'text': '前面的字 ', 'style': []}],
                                       [{'tag': 'img', 'image_key': 'img_v3_02abc_1234-aaaa', 'width': 320, 'height': 120}],
                                       [{'tag': 'text', 'text': ' 后面的字', 'style': []}]]},
             '前面的字 \n![Image](img_v3_02abc_1234-aaaa)\n 后面的字'),
            ({'title': '', 'content': [[{'tag': 'text', 'text': '1. '}, {'tag': 'text', 'text': '第一项'}],
                                       [{'tag': 'text', 'text': '2. '}, {'tag': 'text', 'text': '第二项'}]]},
             '1. 第一项\n2. 第二项'),
        ]
        for post, printed in cases:
            with self.subTest(printed=printed[:20]):
                self.assertEqual(ws_intake.message_event(message(post, 'post'))['content'], printed)
        from covey.feishu.message_parts import collect_message_parts
        texts, images = collect_message_parts(ws_intake.message_event(message(cases[2][0], 'post'))['content'])[:2]
        self.assertEqual(images, ['img_v3_02abc_1234-aaaa'])  # The image is still found and downloaded.
        self.assertEqual(ws_intake.message_event(message('not json', 'post'))['content'], '"not json"')  # Not a post's JSON: as it is.

    def test_text_message_reaches_the_model_text_as_the_words(self):
        from covey.feishu.media import enrich_incoming_event
        event = {**ws_intake.message_event(message({'text': '帮我看下 @_user_1'}, mentions=[
            {'key': '@_user_1', 'id': {'open_id': 'ou_x'}, 'name': 'Alice'}])), REPLY_CONTEXT: True}
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as media:
            enriched = enrich_incoming_event(event, Path(media), None)
        self.assertIn('帮我看下 @Alice', enriched.text)
        self.assertIn('ou_x', enriched.text)  # the mentions block

    def test_card_action_has_the_fields_of_its_jq(self):
        event = ws_intake.card_action_event(CARD)
        names = events.CARD_ACTION_EVENT_JQ.strip('{}').replace(' ', '').split(',')
        self.assertEqual(set(event), set(names))
        self.assertEqual(json.loads(event['action_value']), {'kind': 'stop', 'job': 7})
        self.assertEqual((event['event_id'], event['operator_id'], event['message_id'], event['chat_id'], event['token']),
                         ('ev_1', 'ou_owner', 'om_card', 'oc_1', 'c-fake'))
        self.assertEqual((event['action_tag'], event['action_name'], event['timestamp']),
                         ('button', 'stop_btn', '1790000000123'))
        self.assertIsNone(event['form_value'])
        from covey.app.card_actions import action_value
        self.assertEqual(action_value(event), {'kind': 'stop', 'job': 7})

    def test_card_form_select_and_input(self):
        raw = json.loads(json.dumps(CARD))
        raw['event']['action'].update({'form_value': {'model': 'opus', 'note': '好'}, 'option': 'o1', 'input_value': 'x'})
        event = ws_intake.card_action_event(raw)
        self.assertEqual(json.loads(event['form_value']), {'model': 'opus', 'note': '好'})
        self.assertEqual((event['option'], event['input_value']), ('o1', 'x'))
        from covey.app.card_actions import form_fields
        self.assertEqual(form_fields(event), {'model': 'opus', 'note': '好'})

    def test_menu_has_the_fields_of_its_jq(self):
        event = ws_intake.menu_event(MENU)
        self.assertEqual(set(event), set(events.MENU_EVENT_JQ.strip('{}').replace(' ', '').split(',')))
        self.assertEqual((event['event_key'], event['operator_id'], event['operator_open_id'], event['app_id']),
                         ('covey.board', 'ou_owner', 'ou_owner', 'cli_fake'))
        self.assertEqual((event['timestamp'], event['menu_timestamp']), ('1790000000123', '1790000000000'))  # the click's seconds, as milliseconds

    def test_real_samples_match_lark_cli(self):
        # Captured on a test bot (ids replaced): the SDK's raw events and what lark-cli printed for the same actions.
        text = {'schema': '2.0', 'header': {'event_id': 'e1', 'token': '', 'create_time': '1795139963384',
                                            'event_type': MESSAGE_EVENT_KEY, 'app_id': 'cli_fake'},
                'event': {'sender': {'sender_id': {'open_id': 'ou_owner', 'union_id': 'on_1'}, 'sender_type': 'user'},
                          'message': {'message_id': 'om_1', 'create_time': '1795139962825', 'update_time': '1795139962825',
                                      'chat_id': 'oc_1', 'chat_type': 'p2p', 'message_type': 'text',
                                      'content': '{"text":"T575 对照 A 纯文字 hello"}'}}}
        self.assertEqual(ws_intake.message_event(text), {
            'chat_id': 'oc_1', 'chat_type': 'p2p', 'sender_id': 'ou_owner', 'sender_type': 'user', 'message_type': 'text',
            'content': 'T575 对照 A 纯文字 hello', 'message_id': 'om_1', 'id': 'om_1', 'root_id': None, 'parent_id': None,
            'reply_to': None, 'thread_id': None, 'create_time': '1795139962825', 'update_time': None, 'event_id': 'e1',
            'timestamp': '1795139963384', 'type': MESSAGE_EVENT_KEY, 'mentions': None, 'user_agent': None})
        menu = {'schema': '2.0', 'header': {'event_id': 'e2', 'token': '', 'create_time': '1795139970000',
                                            'event_type': MENU_EVENT_KEY, 'app_id': 'cli_fake'},
                'event': {'operator': {'operator_id': {'open_id': 'ou_owner', 'union_id': 'on_1'}},
                          'event_key': 'bridge_settings', 'timestamp': 1795139970}}
        self.assertEqual(ws_intake.menu_event(menu), {
            'event_id': 'e2', 'event_key': 'bridge_settings', 'operator_id': 'ou_owner', 'operator_open_id': 'ou_owner',
            'timestamp': '1795139970000', 'menu_timestamp': '1795139970000', 'app_id': 'cli_fake'})
        menu['event']['timestamp'] = 1795139970123  # already milliseconds: kept
        self.assertEqual(ws_intake.menu_event(menu)['menu_timestamp'], '1795139970123')

    def test_read_has_the_fields_of_its_jq(self):
        self.assertEqual(ws_intake.read_event(READ), {
            'event_id': 'ev_1', 'reader_id': 'ou_owner', 'read_time': '1790000000500', 'message_ids': ['om_a', 'om_b']})


class IntakeTest(unittest.TestCase):
    def setUp(self):
        ws_intake.reset()
        self.addCleanup(ws_intake.reset)
        self.logs = []
        self.shutdown = threading.Event()
        self.addCleanup(self.shutdown.set)
        self.intake = ws_intake.Intake(SimpleNamespace(profile='fake'), self.shutdown, self.logs.append, 300)
        self.intake.started = True  # No client: the tests feed on_payload and the state by hand.

    def run_consumer(self, key, handle):
        thread = threading.Thread(target=self.intake.consume, args=(key, handle), daemon=True)
        thread.start()
        return thread

    def wait_for(self, check):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not check():
            time.sleep(.01)
        self.assertTrue(check())

    def test_card_callback_answers_a_toast_and_the_click_is_handled(self):
        seen = []
        self.run_consumer(CARD_ACTION_EVENT_KEY, lambda event, ctx: seen.append((event, ctx.profile)))
        self.assertEqual(self.intake.on_payload(frame(CARD)), {'toast': {'type': 'info', 'content': ws_intake.TOAST_TEXT}})
        self.assertEqual(ws_intake.TOAST_TEXT, '收到')
        self.wait_for(lambda: seen)
        self.assertEqual(seen[0][1], 'fake')
        self.assertEqual(json.loads(seen[0][0]['action_value'])['kind'], 'stop')

    def test_toast_does_not_wait_for_the_handler(self):
        release = threading.Event()
        self.addCleanup(release.set)
        self.run_consumer(CARD_ACTION_EVENT_KEY, lambda event, ctx: release.wait(5))
        started = time.monotonic()
        self.assertIsNotNone(self.intake.on_payload(frame(CARD)))
        self.assertLess(time.monotonic() - started, 1)

    def test_other_events_answer_nothing_and_reach_their_own_handler(self):
        got = {}
        for key in (MESSAGE_EVENT_KEY, MENU_EVENT_KEY, READ_EVENT_KEY):
            self.run_consumer(key, lambda event, ctx, key=key: got.__setitem__(key, event))
        for n, raw in enumerate((message({'text': 'hi'}), MENU, READ)):
            raw['header'] = {**raw['header'], 'event_id': f'ev_other_{n}'}  # One ID would count as a repeat.
            self.assertIsNone(self.intake.on_payload(frame(raw)))
        self.wait_for(lambda: len(got) == 3)
        self.assertTrue(got[MESSAGE_EVENT_KEY][REPLY_CONTEXT])
        self.assertEqual(got[MESSAGE_EVENT_KEY]['content'], 'hi')
        self.assertNotIn(REPLY_CONTEXT, got[MENU_EVENT_KEY])
        self.assertEqual(got[READ_EVENT_KEY]['message_ids'], ['om_a', 'om_b'])

    def test_events_before_their_consumer_attaches_wait_for_it(self):
        self.intake.on_payload(frame(MENU))
        seen = []
        self.run_consumer(MENU_EVENT_KEY, lambda event, ctx: seen.append(event))
        self.wait_for(lambda: seen)

    def test_a_repeated_event_id_is_handled_once_but_a_click_still_gets_its_toast(self):
        seen = []
        self.run_consumer(CARD_ACTION_EVENT_KEY, lambda event, ctx: seen.append(event))
        self.assertEqual(self.intake.on_payload(frame(CARD)), ws_intake.TOAST)
        self.assertEqual(self.intake.on_payload(frame(CARD)), ws_intake.TOAST)
        self.wait_for(lambda: seen)
        time.sleep(.2)
        self.assertEqual(len(seen), 1)

    def test_dedup_is_bounded(self):
        with mock.patch.object(ws_intake, 'DEDUP_LIMIT', 3):
            for n in range(5):
                self.assertTrue(self.intake.first_time(f'ev{n}'))
            self.assertEqual(len(self.intake.seen), 3)
            self.assertFalse(self.intake.first_time('ev4'))
            self.assertTrue(self.intake.first_time('ev0'))  # forgotten

    def test_unreadable_or_unknown_events_are_ignored(self):
        self.assertIsNone(self.intake.on_payload(b'not json'))
        self.assertIsNone(self.intake.on_payload(frame(envelope('contact.user.created_v3', {}))))
        self.assertIsNone(self.intake.on_payload(b'[]'))
        self.assertTrue(all(q.empty() for q in self.intake.queues.values()))

    def test_a_handler_error_is_logged_and_the_next_event_is_handled(self):
        calls = []

        def handle(event, ctx):
            calls.append(event['event_key'])
            if len(calls) == 1:
                raise RuntimeError('boom')
        self.run_consumer(MENU_EVENT_KEY, handle)
        self.intake.on_payload(frame(envelope(MENU_EVENT_KEY, MENU['event'], event_id='ev_a')))
        self.intake.on_payload(frame(envelope(MENU_EVENT_KEY, MENU['event'], event_id='ev_b')))
        self.wait_for(lambda: len(calls) == 2)
        self.assertIn('handle_menu_event error: boom', self.logs)

    def test_ready_lines_health_and_disconnect(self):
        for key in (MESSAGE_EVENT_KEY, CARD_ACTION_EVENT_KEY):
            self.run_consumer(key, lambda event, ctx: None)
        self.wait_for(lambda: self.intake.attached >= {MESSAGE_EVENT_KEY, CARD_ACTION_EVENT_KEY})
        self.assertFalse(events.consumers_snapshot()[MESSAGE_EVENT_KEY]['ready'])
        self.intake._set_connected(True)
        self.assertEqual(sorted(line for line in self.logs if 'ready' in line),
                         [f'[event] ready event_key={CARD_ACTION_EVENT_KEY}', f'[event] ready event_key={MESSAGE_EVENT_KEY}'])
        snapshot = events.consumers_snapshot()
        for key in (MESSAGE_EVENT_KEY, CARD_ACTION_EVENT_KEY):
            self.assertTrue(snapshot[key]['ready'])
            self.assertEqual(snapshot[key]['state'], 'ready')
        self.assertEqual(self.intake.status()['status'], 'receiving')
        self.intake._set_connected(False)
        snapshot = events.consumers_snapshot()
        self.assertFalse(snapshot[MESSAGE_EVENT_KEY]['ready'])
        self.assertEqual((snapshot[MESSAGE_EVENT_KEY]['state'], snapshot[MESSAGE_EVENT_KEY]['error']),
                         ('retrying', 'disconnected'))
        self.assertEqual(self.intake.status()['status'], 'disconnected')
        self.intake._set_connected(True)  # a reconnect: ready again, each line again
        self.assertTrue(events.consumers_snapshot()[CARD_ACTION_EVENT_KEY]['ready'])
        self.assertEqual(sum('ready event_key=' in line for line in self.logs), 4)

    def test_a_consumer_attaching_after_the_connection_is_ready_at_once(self):
        self.intake._set_connected(True)
        self.run_consumer(MENU_EVENT_KEY, lambda event, ctx: None)
        self.wait_for(lambda: events.consumers_snapshot().get(MENU_EVENT_KEY, {}).get('ready'))
        self.assertIn(f'[event] ready event_key={MENU_EVENT_KEY}', self.logs)

    def test_ready_line_is_the_one_deploy_and_health_read(self):
        health = events.ConsumerHealth(MENU_EVENT_KEY)
        health.stderr(f'[event] ready event_key={MENU_EVENT_KEY}')
        self.assertIsNotNone(health.ready_at)
        self.assertEqual(events.format_event_stderr(f'[event] ready event_key={MENU_EVENT_KEY}'),
                         f'[event] ready event_key={MENU_EVENT_KEY}')

    def test_connection_status_feeds_the_connections_thread(self):
        from covey.feishu import connections
        runtime = SimpleNamespace(shutdown=threading.Event(), connection_health={}, log=lambda line: None)
        runtime.shutdown.wait = lambda seconds: runtime.shutdown.set() or True
        ws_intake._intake = self.intake
        self.intake._set_connected(True)
        with mock.patch.dict(os.environ, {'COVEY_EVENT_TRANSPORT': 'sdk'}), \
                mock.patch.object(connections, 'bus_sample', side_effect=AssertionError('lark-cli bus asked')):
            connections.watch(runtime, SimpleNamespace(profile='fake'))
        self.assertEqual(runtime.connection_health['status'], 'receiving')
        self.assertEqual(runtime.connection_health['transport'], 'sdk')


class SwitchTest(unittest.TestCase):
    def test_enabled_by_the_switch_alone(self):
        for value, expected in (('sdk', True), ('SDK', True), ('cli', False), ('', False)):
            with mock.patch.dict(os.environ, {'COVEY_EVENT_TRANSPORT': value}):
                self.assertEqual(ws_intake.enabled(), expected, value)
        with mock.patch.dict(os.environ, clear=False):
            os.environ.pop('COVEY_EVENT_TRANSPORT', None)
            os.environ.pop('LARK_BRIDGE_EVENT_TRANSPORT', None)
            self.assertFalse(ws_intake.enabled())

    def test_off_consume_runs_the_lark_cli_consumer_as_before(self):
        ctx = SimpleNamespace(profile='bot')
        env = {k: v for k, v in os.environ.items() if k not in ('COVEY_EVENT_TRANSPORT', 'LARK_BRIDGE_EVENT_TRANSPORT')}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(events, 'consume_event_stream') as stream:
            events.consume(ctx, CARD_ACTION_EVENT_KEY, handle=lambda e, c: None, set_process=lambda p: None,
                           shutdown=threading.Event(), log=lambda line: None, retry_seconds=300)
        argv = stream.call_args.kwargs['argv']
        self.assertEqual(argv[argv.index('consume') + 1:], [CARD_ACTION_EVENT_KEY, '--as', 'bot', '--jq',
                                                            events.CARD_ACTION_EVENT_JQ])
        self.assertEqual(stream.call_args.kwargs['start_log'], 'starting card action event consumer')

    def test_on_the_bridge_starts_no_lark_cli_consumer(self):
        from covey.app.unified import UnifiedBridgeApp
        bridge = SimpleNamespace(runtime=SimpleNamespace(shutdown=threading.Event(), log=lambda line: None), event_procs={})
        bridge.runtime.shutdown.set()
        self.addCleanup(ws_intake.reset)
        with mock.patch.dict(os.environ, {'COVEY_EVENT_TRANSPORT': 'sdk'}), \
                mock.patch('covey.app.unified.consume', side_effect=AssertionError('lark-cli consumer')), \
                mock.patch.object(ws_intake.Intake, '_start') as start:
            UnifiedBridgeApp.consume_events(bridge, SimpleNamespace(profile='bot'), MENU_EVENT_KEY, lambda e, c: None)
        start.assert_called_once_with()

    def test_off_the_bridge_runs_the_lark_cli_consumer(self):
        from covey.app.unified import UnifiedBridgeApp
        bridge = SimpleNamespace(runtime=SimpleNamespace(shutdown=threading.Event(), log=lambda line: None), event_procs={})
        env = {k: v for k, v in os.environ.items() if k not in ('COVEY_EVENT_TRANSPORT', 'LARK_BRIDGE_EVENT_TRANSPORT')}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch('covey.app.unified.consume') as lark_cli, \
                mock.patch.object(ws_intake, 'consume', side_effect=AssertionError('sdk')):
            UnifiedBridgeApp.consume_events(bridge, SimpleNamespace(profile='bot'), MENU_EVENT_KEY, lambda e, c: None)
        lark_cli.assert_called_once()

    def test_off_imports_no_lark_oapi(self):
        code = ("import sys; import covey.feishu.events, covey.feishu.ws_intake, covey.feishu.connections, "
                "covey.app.health; assert not any(m == 'lark_oapi' or m.startswith('lark_oapi.') for m in sys.modules)")
        env = {k: v for k, v in os.environ.items() if k not in ('COVEY_EVENT_TRANSPORT', 'LARK_BRIDGE_EVENT_TRANSPORT')}
        proc = subprocess.run([sys.executable, '-B', '-c', code], cwd=ROOT, env={**env, 'PYTHONPATH': str(ROOT)},
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_health_names_the_transport_only_in_sdk_mode(self):
        from covey.app.health import bridge_snapshot
        runtime = SimpleNamespace(connection_health={}, availability_health={}, shutdown=threading.Event(),
                                  jobs=SimpleNamespace(queue=SimpleNamespace(qsize=lambda: 0)))
        with mock.patch.dict(os.environ, {'COVEY_EVENT_TRANSPORT': 'sdk'}):
            self.assertEqual(bridge_snapshot(runtime, {})['event_transport'], 'sdk')
        with mock.patch.dict(os.environ, {'COVEY_EVENT_TRANSPORT': 'cli'}):
            self.assertNotIn('event_transport', bridge_snapshot(runtime, {}))


@unittest.skipIf(importlib.util.find_spec('lark_oapi') is None, 'lark_oapi is not installed')
class SdkContractTest(unittest.TestCase):
    """What ws_intake leans on in lark-oapi 1.7.4: the dispatcher's raw hook, the client's connection and flags."""

    def test_client_names_what_the_intake_reads(self):
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
            from lark_oapi.ws import client as ws_client
        handler = EventDispatcherHandler.builder('', '').build()
        self.assertTrue(callable(handler._do_without_validation))
        client = ws_client.Client('cli_fake', 'fake', event_handler=handler)
        self.assertIsNone(client._conn)
        self.assertTrue(client._auto_reconnect)
        self.assertTrue(hasattr(ws_client, 'loop') and callable(client._disconnect))

    def test_connection_thread_with_a_fake_client(self):
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            from lark_oapi.ws import client as ws_client
        ws_intake.reset()
        self.addCleanup(ws_intake.reset)
        shutdown, logs, answered, handled = threading.Event(), [], [], []
        self.addCleanup(shutdown.set)

        class FakeClient:
            def __init__(self, app_id, secret, log_level=None, event_handler=None, domain=None):
                self.args, self._conn, self.handler = (app_id, secret, domain), None, event_handler

            def start(self):
                self._conn = object()  # connected
                answered.append(self.handler._do_without_validation(frame(CARD)))
                shutdown.wait(5)

        creds = SimpleNamespace(config=SimpleNamespace(app_id='cli_fake', app_secret='fake', domain='https://open.example'))
        with mock.patch.object(ws_client, 'Client', FakeClient), \
                mock.patch('covey.feishu.sdk.client', return_value=creds) as credentials, \
                mock.patch.object(ws_intake, 'POLL_SECONDS', .02):
            consumer = threading.Thread(target=ws_intake.consume, args=(SimpleNamespace(profile='bot'), CARD_ACTION_EVENT_KEY),
                                        kwargs=dict(handle=lambda event, ctx: handled.append(event), shutdown=shutdown,
                                                    log=logs.append, retry_seconds=300), daemon=True)
            consumer.start()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not (handled and any('ready' in line for line in logs)):
                time.sleep(.02)
            shutdown.set()
            consumer.join(5)
        credentials.assert_called_once_with('bot')
        self.assertEqual(answered, [ws_intake.TOAST])
        self.assertEqual(len(handled), 1)
        self.assertIn(f'[event] ready event_key={CARD_ACTION_EVENT_KEY}', logs)


if __name__ == '__main__':
    unittest.main()
