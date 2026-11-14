"""The proxy reply tests' shared case and helpers (test_proxy, test_proxy_panel, test_proxy_send): ProxyCase
(a unified app with 代答 set up, one contact, no real lark-cli or CardKit), messages as lark-cli lists them, a
user_cli stand-in, and the controls of a card."""
import contextlib
import functools
import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support.fake_lark import FakeCardKit
from support.world import UnifiedCase
from covey.app import proxy as proxy_module
from covey.core import proxy as records
from covey.core.jobs import upsert_job_record

OWNER = 'ou_me'
PERSON = {'key': 'p2p:ou_zhang', 'kind': 'p2p', 'target': 'ou_zhang', 'name': '张三', 'chat_id': 'oc_p2p',
          'paused': False, 'since': 0, 'added_at': 1, 'seen': [], 'session_dir': '/x/misc-tasks/sessions/s'}
GROUP = {'key': 'group:oc_g', 'kind': 'group', 'target': 'oc_g', 'name': '项目群', 'chat_id': 'oc_g',
         'paused': False, 'since': 0, 'added_at': 2, 'seen': []}
SENT = {'id': 'om_sent', 'time': time.strftime('%Y-%m-%d 09:41')}  # The owner's message a send is read back as.
# The send tool's receipt (stdout, over many lines) and its error line (stderr), as send-as-user.py prints them.
RECEIPT = (json.dumps({'ok': True, 'identity': 'user', 'data': {'message_id': 'om_tool', 'create_time': '1761615660000',
                                                                'web_message_id': '7301'}}, indent=2), [])


def failure(kind, message='原因'):
    return '', ['Installed 3 packages\n', json.dumps({'ok': False, 'identity': 'user', 'error': {'type': kind, 'message': message}}) + '\n']


def installed_tool(folder):
    """the sender tool as scripts/install_send_as_user.sh leaves it (a stand-in script, its SHA256SUMS)."""
    script = Path(folder) / 'scripts' / 'send-as-user.py'
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text('# stand-in\n')
    (Path(folder) / 'SHA256SUMS').write_text(hashlib.sha256(script.read_bytes()).hexdigest() + '  ./scripts/send-as-user.py\n')
    return str(folder)


@contextlib.contextmanager
def owner_cli(**fake):
    """user_cli (the owner's lark-cli reads) faked as one mock wherever 代答 calls it: polling, commands, the web side."""
    cli = mock.Mock(**fake)
    with contextlib.ExitStack() as stack:
        for module in ('proxy_intake', 'proxy_commands', 'proxy_send'):
            stack.enter_context(mock.patch(f'covey.app.{module}.user_cli', cli))
        yield cli


def item(mid, content, *, chat='oc_p2p', sender='ou_zhang', position='7', mentions=(), name='张三', at=None,
         kind='text', thread='', chat_type=''):
    """A message as lark-cli lists it, sent ``at`` (epoch seconds, now by default). The search gives its
    ``chat_type`` (p2p or group), the list none: left out unless given."""
    return {'message_id': mid, 'chat_id': chat, 'content': content, 'message_position': position,
            'create_time': time.strftime('%Y-%m-%d %H:%M', time.localtime(time.time() if at is None else at)),
            'msg_type': kind, 'sender': {'id': sender, 'name': name}, 'thread_id': thread,
            'mentions': [{'id': m} for m in mentions], **({'chat_type': chat_type} if chat_type else {})}


def lark(users=(), chats=(), *, info=None, members=None, mentions=None, delay=0.0, fail=None):
    """A user_cli stand-in answering by what is asked, in any order (searches run in parallel):
    people and groups (a list for every query, or {query: list}), chat info {chat_id: data}, a
    group's member list {chat_id: data}, the mentions of a message read back as the owner.
    ``calls`` are the argument tuples it got."""
    calls = []

    def pick(table, key):
        return list(table.get(key, [])) if isinstance(table, dict) else list(table)

    def answer(profile, *args, **kwargs):
        calls.append(args)
        if delay:
            time.sleep(delay)
        if fail:
            raise fail
        if args[:2] == ('contact', '+search-user'):
            return {'data': {'users': pick(users, args[3])}}
        if args[:2] == ('im', '+chat-search'):
            return {'data': {'chats': pick(chats, args[3])}}
        if args[:2] == ('api', 'GET'):
            return {'data': (info or {}).get(args[2].rsplit('/', 1)[1], {})}
        if args[:2] == ('im', '+chat-members-list'):
            return {'data': (members or {})[args[args.index('--chat-id') + 1]]}
        if args[:2] == ('im', '+messages-mget'):
            return {'data': {'messages': [{'message_id': args[3], 'mentions': list(mentions or [])}]}}
        raise AssertionError(f'unexpected lark-cli call {args}')
    answer.calls = calls
    return answer


def user(open_id, name, **fields):
    return {'open_id': open_id, 'localized_name': name, **fields}


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def controls(card):
    """(kind, label or select, the rest of its value) of every control on a 代答设置 card."""
    found = []
    for element in walk(card):
        for behavior in element.get('behaviors') or []:
            value = dict(behavior['value'])
            label = element['text']['content'] if element.get('tag') == 'button' else element['tag']
            found.append((value.pop('kind'), label, value, element))
    return found


class ProxyCase(UnifiedCase):
    def setUp(self):
        super().setUp()
        self.proxy = self.app.proxy
        self.state = self.ctx.state_dir
        records.write_settings(self.state, {
            'enabled': True, 'group_chat_id': 'oc_proxy', 'user_profile': 'me', 'owner_user_id': OWNER,
            'owner_name': '胥可', 'harness': 'codex', 'model': 'codex-model', 'effort': 'high',
            'workspace': 'project', 'workdir': str(self.work), 'web_python': '/py', 'web_home': str(self.root / 'web'),
            'send_tool': installed_tool(self.root / 'tool')})
        records.write_contact(self.state, dict(PERSON))
        self.cards = iter(['om_root', 'om_mirror', 'om_mirror2'])
        self.app.messenger.create_and_send_card = mock.Mock(side_effect=lambda *a, **k: ('card', next(self.cards)))
        # Any other card (the 重新发送 card): CardKit faked, and never called under Proxy.lock (rule 1).
        self.lark = FakeCardKit({'code': 0, 'data': {'card_id': 'rcid'}}, sent=('om_resend', False)).start(self)
        for fake in (self.lark.api, self.lark.send):
            fake.side_effect = functools.partial(self.unlocked, fake)
        # No real lark-cli: a read a test does not answer fails as an unreachable Feishu would.
        self.enterContext(owner_cli(side_effect=RuntimeError('lark-cli not stubbed')))
        self.proxy.web_login.status_next = float('inf')  # No +status of the tool unless a test asks for one (F4).
        self.enterContext(mock.patch.object(proxy_module, 'SETTLE_SECONDS', 0, create=True))  # Unless a test waits for it.

    def unlocked(self, fake, *args, **kwargs):
        if self.proxy.lock.locked():
            raise AssertionError('a Feishu call under Proxy.lock')
        return fake.return_value

    def settings(self):
        return self.proxy.settings(self.ctx)

    def listen_round(self, *, search):
        """``listen_once``, its search (a thread of its own) waited for."""
        self.proxy.intake.listen_once(self.ctx, self.settings(), search=search)
        if search and self.proxy.intake.searching is not None:
            self.proxy.intake.searching.join(10)

    def tool_runs(self, *answers):
        """The send tool's runs faked (ProxySend.run_tool): each answers the next of ``answers`` (stdout, stderr's
        lines). ``runs`` keeps each one's arguments, folder, time limit and the text it was given."""
        runs = []

        def run(settings, args, *, cwd, timeout, on_line=None, **_):
            text = (Path(cwd) / 'reply.txt').read_text(encoding='utf-8')
            runs.append(SimpleNamespace(args=list(args), cwd=cwd, timeout=timeout, text=text))
            return answers[len(runs) - 1]
        return mock.patch.object(self.proxy.web, 'run_tool', side_effect=run), runs

    def panel_chat(self):
        """The owner's newest 代答设置 card is in oc_dm."""
        panels = records.proxy_dir(self.state) / 'panels'
        panels.mkdir(parents=True, exist_ok=True)
        (panels / 'tok.json').write_text(json.dumps({'chat_id': 'oc_dm'}))
        (panels / 'latest.json').write_text(json.dumps({'token': 'tok'}))

    def take_in(self, *items):
        with owner_cli(return_value={'data': {'messages': list(items)}}):
            self.search_in()

    def search_in(self):
        """The search's new messages made batches now, on this thread (``listen`` hands them to ``run``)."""
        settings, contacts = self.settings(), records.read_contacts(self.state)
        for key, messages in self.proxy.intake.new_messages(settings, contacts, self.proxy.intake.search(settings, contacts)).items():
            self.proxy.add_messages(self.ctx, key, messages)

    def batches(self, statuses=None):
        return records.read_batches(self.state, statuses=statuses)

    def submitted(self, status='done', at=None):
        self.take_in(item('m1', '接口文档在哪', at=at))
        [batch] = self.batches()
        self.proxy.submit(self.ctx, self.settings(), records.read_contacts(self.state)[0], batch)
        [batch] = self.batches()
        upsert_job_record(self.state, batch['job_key'], status=status, job_id=batch['job_id'], topic_id=batch['topic_id'],
                          message_id='om_mirror', chat_id='oc_proxy')
        return batch

    def run_tick(self, reply, web, read_back=None, owner_replied=False):
        """A poll with the model's ``reply`` and the page's result ``web``; ``read_back`` is the owner's
        message the send is found as (om_sent, sent at 09:41 today, by default; {} for none)."""
        read_back = SENT if read_back is None else read_back
        with mock.patch.object(self.proxy, 'reply_of', return_value=reply), \
             mock.patch.object(self.proxy.web, 'owner_replied', return_value=owner_replied), \
             mock.patch.object(self.proxy.web, 'web_send', return_value=web) as sent, \
             mock.patch.object(self.proxy.web, 'read_back', return_value=read_back), \
             mock.patch.object(self.proxy.web_login, 'check_login'):
            self.proxy.tick(self.ctx, self.settings())
        return sent

    def notes(self):
        return [c.args[2] for c in self.app.messenger.send_message.call_args_list]

    def owner_turn(self, batch, step=1):
        upsert_job_record(self.state, f'owner-turn-{step}', status='done', job_id=batch['job_id'] + step,
                          topic_id=batch['topic_id'], message_id=f'om_owner{step}', chat_id='oc_proxy')
