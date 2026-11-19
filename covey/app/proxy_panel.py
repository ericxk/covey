"""The 代答设置 card: the owner's private-chat panel for 代答 (app/proxy_commands.py does what it asks).

The bot menu's 代答设置 (app/bot_menu.py) sends one standalone card to the owner's private chat:
no topic, no job. Each control's value carries a ``bridge_proxy_*`` kind (app/card_actions.py), the
panel's token and the revision it was drawn at. A click is decided under the panel's lock, records
change through Proxy (Proxy.lock, core/proxy.py), and the whole card is drawn again: never one
element inside its form. A refusal or error is written on the card and cleared by the next action.

- Only the newest panel acts: a click on an older one turns that card into an expired notice.
- A click drawn at an older revision of this one still acts while what it names is still there
  (the search result it came from, the contact, the group's members, the confirmation): a quick
  second click is not lost. One whose action no longer applies is refused on the card (proxy_stale).
- A successful add takes the search result off the card (``spent`` keeps it for such a second
  click, until the next 查找). A whitelist long enough to be folded (cards/proxy_panel.py FOLD_ROWS) is
  drawn open after a click on one of its rows (``whitelist_open``), folded after any other. 逐个管理
  on a group's members' row lists them, each with its own 删除, until clicked again (``source_open``).
- 开启 (it may read the owner's identity and create the proxy group), 查找 (searches as the owner),
  读取群成员 and 确认添加 (writes a contact a member) return at once: the work runs after the click
  (after_ack), outside the lock, while the busy card is drawn beside it; then the result is drawn.
  Both writes take their sequence from ``reserve`` under the lock, the result after the busy card.

The card's record is <state>/proxy/panels/<token>.json (``latest.json`` names the newest panel);
the card is cards/proxy_panel.py. On the bot menu's 设置 card it is the 代答 tab (app/settings_hub.py): the 设置
card's record holds the tab's view (``path`` finds it by the token), and turning the card to the tab makes that card
the newest panel; expired there, it keeps its tab row.
"""
from __future__ import annotations

import functools
import re
import subprocess
import threading
import uuid

from covey.app.card_actions import action_label, form_fields
from covey.app.card_lanes import after_ack
from covey.app.form_cards import replace_form_card, save_record
from covey.app.launch_models import available_models, run_choice
from covey.core.menu_choice import budget_effort, effort_options, find_choice, harness_of, tier_pick, tiers
from covey.app.menu_panel import OPEN, MenuPanel, hub_top
from covey.cards.settings_hub import PROXY
from covey.cards.proxy_panel import (ADD, ADD_ALL, BUDGET, DISABLE, ENABLE, FIND, HARNESS, KINDS, LOGIN,
                                              MEMBERS, MEMBERS_ADD, MEMBERS_CANCEL, MODEL, PAGE, PAUSE, PROVIDER,
                                              QUERY_LIMIT, REFRESH, REMOVE, RESUME, SOURCE_OPEN, SOURCE_PAUSE,
                                              SOURCE_REMOVE, SOURCE_RESUME, TIER_BUDGET, TIER_MODEL, addable,
                                              expired_card, folded_card, lookup_of, proxy_panel_card)
from covey.core import proxy as records
from covey.core import timing
from covey.core.state import read_json

EXPIRED = 'expired'
LATEST = 'latest.json'
OWNER_ONLY = '⚠️ 仅机器人主人可以修改代答设置。'
STALE = '⚠️ 卡片刚更新过，这次点击没有执行：'
GONE = '白名单里已经没有这一项（可能已被删除），卡片已按最新记录重画。'
REPLACED = '这个查找结果已被新的查找替换，请按现在的结果操作。'
NO_PROFILE = '⚠️ 未配置以你身份查找联系人的 lark-cli profile（COVEY_PROXY_PROFILE）。'
WHITELIST = frozenset({PAUSE, RESUME, REMOVE, SOURCE_OPEN, SOURCE_PAUSE, SOURCE_RESUME, SOURCE_REMOVE, PAGE})  # Its rows' controls.
FAILURES = (RuntimeError, ValueError, OSError, subprocess.SubprocessError)


class Refused(Exception):
    """A click whose action no longer applies; why, written on the card."""


def short(text, limit=30):
    text = ' '.join(str(text).split())
    return text if len(text) <= limit else text[:limit] + '…'


class ProxyPanel:
    def __init__(self, proxy):
        self.proxy = proxy
        # Panel token -> the busy notes of its clicks whose work has not finished, oldest first: the
        # card shows the first. Changed under the panel's lock; empty again after a restart.
        self.working = {}
        self.menu = MenuPanel(proxy, 'proxy-panel', lambda state_dir: records.proxy_dir(state_dir) / 'panels',
                              'proxy-panel', tab=PROXY)

    def folder(self, ctx):
        return self.menu.directory(ctx)

    def path(self, ctx, token):
        return self.menu.path(ctx, token)

    def latest(self, ctx):
        try:
            return str(read_json(self.folder(ctx) / LATEST).get('token') or '')
        except (OSError, ValueError, AttributeError):
            return ''

    def owner_chat(self, ctx):
        """The owner's private chat with the bot: the newest panel's (one opens only there); '' before any."""
        token = self.latest(ctx)
        try:
            return str(read_json(self.path(ctx, token)).get('chat_id') or '') if token else ''
        except (OSError, ValueError, AttributeError):
            return ''

    # Opening ---------------------------------------------------------------------------------

    def opened(self, ctx, token):
        """Panel ``token`` is the newest: the one that acts."""
        save_record(self.folder(ctx) / LATEST, {'token': token})
        self.proxy.runtime.log(f'proxy panel opened token={token[:8]}')

    def open(self, ctx, chat_id, source, shown=None):
        """Send a new panel to ``chat_id`` (the owner's private chat); it becomes the one that acts.
        ``source``: the menu click's event_id, which keys the card; ``shown`` hears its message id."""
        self.menu.open(ctx, chat_id, source, lambda token, record: self.card(ctx, token, record['view']),
                       '代答卡未能发送，请稍后再点机器人菜单「代答」。', view=lambda token: {'revision': 1},
                       opened=functools.partial(self.opened, ctx), shown=shown)

    def fold(self, ctx, chat_id, source, message_id):
        """A second click on the menu's 代答 while its card is the newest (app/bot_menu.py): the card folded, or
        drawn again as it opens (and the newest panel again)."""
        return self.menu.toggle(ctx, chat_id, source, message_id,
                                lambda token, record: self.card(ctx, token, record['view']),
                                lambda token, record: folded_card(), shown=functools.partial(self.opened, ctx))

    # The 设置 card's 代答 tab (app/settings_hub.py): its first view, its record's fields, its card; once shown it
    # is the newest panel, as one opened.
    @staticmethod
    def tab_view(token):
        return {'revision': 1, 'hub': True}

    @staticmethod
    def tab_fields(ctx):
        return {}

    def tab_card(self, ctx, token, record):
        return self.card(ctx, token, record['view'])

    def tab_shown(self, ctx, token):
        self.opened(ctx, token)

    def refuse(self, ctx, path, event):
        """OWNER_ONLY on the card ``event`` clicked; whether it was a 代答 card's."""
        return self.menu.refuse(ctx, path, event, OWNER_ONLY, lambda view, record: self.card(ctx, path.stem, view))

    # Drawing ---------------------------------------------------------------------------------

    def current(self, settings, choices):
        """(menu key, budget option) of the settings' model; None when the menu does not offer it."""
        found = find_choice(choices, f"{settings.get('harness') or ''}:{settings.get('model') or ''}")
        if not found:
            return None
        key, budget = found  # A budget preset comes with its budget; any other model takes the settings'.
        effort = budget or settings.get('effort') or 'default'
        options = dict(effort_options(choices[key])[0])
        return key, effort if effort in options else ''

    def card(self, ctx, token, view):
        proxy = self.proxy
        settings = proxy.settings(ctx)
        settings['send_problems'] = proxy.web.problems(ctx, settings)
        choices = available_models(proxy.topics, ctx)
        return proxy_panel_card(token, view, settings=settings, contacts=records.read_contacts(ctx.state_dir),
                                batches=records.read_batches(ctx.state_dir), choices=choices,
                                current=self.current(settings, choices), busy=(self.working.get(token) or [''])[0],
                                top=hub_top(PROXY, token, view))

    def publish(self, ctx, path, record, view):
        """The whole card drawn for ``view``, saved with it; False when CardKit did not confirm (the
        view is saved anyway: the next click redraws it)."""
        return self.menu.publish(ctx, path, record, view, self.card(ctx, path.stem, view))

    def redraw(self, ctx, path):
        """The card as its record says now: the busy note while a slow click's work runs."""
        with self.menu.lock(path):
            try:
                record = read_json(path)
            except (OSError, ValueError):
                return
            if self.menu.shows(record):
                self.publish(ctx, path, record, dict(record['view']))

    def unsaved(self, ctx, record, view, notice):
        """The change happened but the card still shows the view before it: say so under the card."""
        self.menu.unsaved(ctx, record, (notice or '改好了') + '\n代答卡未能刷新，请点卡上的“刷新”。',
                          f"{record['card_id']}-{view['revision']}", reply_to_message_id=record['message_id'])

    # Clicks ----------------------------------------------------------------------------------

    @staticmethod
    def matches(value):
        return value.get('kind') in KINDS

    def claim(self, ctx, event, raw, owner):
        """The click's outcome when it is a 代答设置 card's, else None (app/unified.py PANELS): no topic,
        never a task runner's."""
        return self.click(ctx, event, raw, owner) if self.matches(raw) else None

    def click(self, ctx, event, value, owner):
        """A click on a 代答设置 card; the outcome its ``card action`` line records."""
        token, revision = value.get('token'), value.get('revision')
        if (not isinstance(token, str) or not re.fullmatch('[0-9a-f]{24}', token)
                or not isinstance(revision, int) or isinstance(revision, bool)):
            return 'proxy_unknown'
        path = self.path(ctx, token)
        later = follow = None
        outcome = 'proxy_panel'
        with self.menu.lock(path):
            record = self.menu.clicked(path, event, (OPEN, EXPIRED))
            if record is None:
                return 'proxy_unknown'
            if record['status'] == EXPIRED:
                return 'proxy_expired'  # Its card already says so; nothing on it to click.
            view = dict(record['view'])
            if not owner:
                self.publish(ctx, path, record, {**view, 'revision': view['revision'] + 1, 'notice': OWNER_ONLY})
                return 'owner_only'
            if self.latest(ctx) != token:
                replace_form_card(ctx, path, record, {**record, 'status': EXPIRED},
                                  expired_card(hub_top(PROXY, token, view)), self.proxy.messenger, 'proxy-panel')
                return 'proxy_expired'
            kind = value['kind']
            old = revision != view['revision']
            view['revision'] += 1
            if kind != REFRESH:
                view['whitelist_open'] = kind in WHITELIST
            slow, notice = None, ''
            try:
                slow = self.slow(ctx, kind, event, value, view)
                if slow is None:
                    notice = self.act(ctx, kind, event, value, record, view, old)
            except Refused as exc:
                notice = (STALE if old else '⚠️ ') + str(exc)
                outcome = 'proxy_stale' if old else outcome
            except FAILURES as exc:
                notice = f'⚠️ {action_label(kind)}未完成：{exc}'
            if slow is not None:
                view['notice'] = ''
                self.working.setdefault(token, []).append(slow[0])
                save_record(path, {**record, 'view': view})  # Drawn by the work's own busy card.
                follow = functools.partial(self.finish, ctx, path, kind, slow[1])
            else:
                view['notice'] = notice
                if not self.publish(ctx, path, record, view) and notice:
                    later = record, view, notice
        if follow:
            after_ack(follow)  # Outside the lock: off a card lane it runs right here.
        if later:
            self.unsaved(ctx, *later)
        return outcome

    @staticmethod
    def query(event):
        query = (form_fields(event) or {}).get('query')
        return query.strip() if isinstance(query, str) and len(query) <= QUERY_LIMIT else ''

    def slow(self, ctx, kind, event, value, view):
        """(busy note, work) of a click whose work runs after it returns; None for one that runs now
        (a search with nothing to search runs now: it is refused)."""
        proxy = self.proxy
        if kind == ENABLE:
            if proxy.settings(ctx).get('enabled'):
                raise Refused('代答已经是开启的。')
            return '⏳ 正在开启代答…', functools.partial(self.enable, ctx)
        if kind == FIND:
            query = self.query(event)
            if not query or not proxy.settings(ctx).get('user_profile'):
                return None
            view['query'] = query
            return f'⏳ 正在查找“{short(query)}”…', functools.partial(self.find, ctx, query)
        if kind == MEMBERS:
            chat_id, name = value.get('group'), value.get('name')
            if not isinstance(chat_id, str) or not chat_id.startswith('oc_') or not isinstance(name, str):
                raise Refused('这个群的信息不全，请重新查找。')
            if not proxy.settings(ctx).get('user_profile'):
                return None
            return f'⏳ 正在读取群「{short(name)}」的成员…', functools.partial(self.members, ctx, chat_id, name)
        if kind == MEMBERS_ADD:
            members = view.get('members')
            if not members or value.get('pending') != members.get('id'):
                raise Refused('这个确认已失效，请重新点“加入全体成员”。')
            view['members'] = None  # A second 确认 finds nothing to confirm.
            return f"⏳ 正在添加群「{short(members['name'])}」的 {len(members['add'])} 人…", \
                functools.partial(self.add_members, ctx, members)
        return None

    def act(self, ctx, kind, event, value, record, view, old):
        """Do a click that runs now; the notice the card shows. Refused when it no longer applies."""
        proxy = self.proxy
        if kind == REFRESH:
            return ''
        if kind == DISABLE:
            if not proxy.settings(ctx).get('enabled'):
                raise Refused('代答已经是关闭的。')
            return '✓ ' + proxy.commands.cmd_disable(ctx, '', '', '')
        if kind in (FIND, MEMBERS):
            if not proxy.settings(ctx).get('user_profile'):
                return NO_PROFILE
            return '⚠️ 请先在输入框里填要查找的人名、邮箱、群名，或 ou_/oc_ 开头的 ID（可写多个）。'
        if kind in (HARNESS, PROVIDER, TIER_MODEL, TIER_BUDGET):
            return self.pick(ctx, kind, value.get('option'))
        if kind in (MODEL, BUDGET):  # A select of a card drawn before the rows: its option beside the value.
            return self.choose(ctx, kind, event.get('option'))
        if kind == PAGE:
            page = value.get('page')
            if not isinstance(page, int) or isinstance(page, bool):
                raise Refused('翻页按钮不完整，请点“刷新”。')
            view['page'] = max(0, page)
            return view.get('notice') or ''
        if kind == ADD:
            notice = '✓ ' + proxy.commands.add_contact(ctx, proxy.settings(ctx), self.candidate(view, value, old))
            self.spend(view)
            return notice
        if kind == ADD_ALL:
            lookup = self.searched(view, value)
            if not lookup:
                raise Refused(REPLACED)
            ready = addable(lookup, {c['key'] for c in records.read_contacts(ctx.state_dir)})
            if not ready:
                raise Refused('查找结果里的唯一匹配都已在白名单。')
            settings = proxy.settings(ctx)
            added, _ = proxy.commands.add_contacts(ctx, settings, ready)
            self.spend(view)
            return (f'✓ 已加入白名单 {len(added)} 个：' + '、'.join(
                c['name'] + ('（群里 @你的消息）' if c['kind'] == 'group' else '') for c in added)
                + '。从现在起的新消息会自动代答' + ('。' if settings.get('enabled') else '；代答目前是关闭的，开启后生效。'))
        if kind == MEMBERS_CANCEL:
            members = view.get('members')
            if not members or value.get('pending') != members.get('id'):
                raise Refused('这个确认已经不在卡上了。')
            view['members'] = None
            return f"已取消，群「{members['name']}」的成员一个也没有加。"
        if kind == LOGIN:
            return proxy.commands.cmd_login(ctx, '', record['chat_id'], record['message_id'],
                                   key=f"{record['card_id']}-{view['revision']}")
        if kind == SOURCE_OPEN:
            chat_id = value.get('source')
            if not any(records.source_of(c) == chat_id for c in records.read_contacts(ctx.state_dir)):
                raise Refused(GONE)
            view['source_open'] = None if view.get('source_open') == chat_id else chat_id
            return ''
        if kind in (SOURCE_PAUSE, SOURCE_RESUME, SOURCE_REMOVE):
            return self.source(ctx, kind, value.get('source'))
        key = value.get('contact')
        contact = next((c for c in records.read_contacts(ctx.state_dir) if c['key'] == key), None)
        if contact is None:
            raise Refused(GONE)
        if kind == REMOVE:
            return '✓ ' + proxy.commands.cmd_remove(ctx, key, '', '')
        if bool(contact.get('paused')) == (kind == PAUSE):
            raise Refused(f"{contact['name']} 已经是{'暂停' if kind == PAUSE else '运行中'}的了。")
        return '✓ ' + proxy.commands.set_paused(ctx, key, kind == PAUSE)

    def source(self, ctx, kind, chat_id):
        """暂停全部, 恢复全部 or 删除全部 on the members of one group."""
        proxy = self.proxy
        if not isinstance(chat_id, str) or not chat_id:
            raise Refused(GONE)
        if kind == SOURCE_REMOVE:
            name, removed = proxy.commands.remove_source(ctx, chat_id)
            if not removed:
                raise Refused(GONE)
            return f'✓ 已把群「{name}」的 {removed} 位成员移出白名单。他们的话题保留，之后的消息不再代答。'
        pause = kind == SOURCE_PAUSE
        name, total, changed = proxy.commands.set_source_paused(ctx, chat_id, pause)
        if not total:
            raise Refused(GONE)
        if not changed:
            raise Refused(f"群「{name}」的成员已经都{'暂停' if pause else '在运行'}了。")
        return (f'✓ 群「{name}」的成员：{changed} 人' + ('已暂停代答。' if pause else '已恢复代答（只回恢复之后的新消息）。'))

    @staticmethod
    def spend(view):
        """An add went through: the search result leaves the card, kept only for a click drawn with it."""
        view.update(spent=lookup_of(view) or view.get('spent'), lookup=None, found=None)

    @staticmethod
    def searched(view, value):
        """The search result a click on it names: the one on the card, or the one an add took off it."""
        return next((lookup for lookup in (lookup_of(view), view.get('spent'))
                     if lookup and lookup['id'] and value.get('lookup') == lookup['id']), None)

    def candidate(self, view, value, old):
        """The candidate an 添加 names: by its key in the search result it was drawn with."""
        if 'candidate' not in value:  # A card drawn before several names: the index of its candidate.
            lookup = lookup_of(view)
            items = (lookup or {}).get('items') or []
            found = items[0]['candidates'] if lookup and not lookup['id'] and items else []
            index = value.get('index')
            if old or not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(found):
                raise Refused('这个查找结果已失效，请重新查找。')
            return found[index]
        lookup = self.searched(view, value)
        if not lookup:
            raise Refused(REPLACED)
        found = next((c for item in lookup['items'] for c in item['candidates'] if c['key'] == value['candidate']), None)
        if found is None:
            raise Refused('这个查找结果已失效，请重新查找。')
        return found

    def pick(self, ctx, kind, option):
        """A tap on a model row (cards/proxy_panel.py model_rows), as on the launch card (app/model_picker.py): a
        Harness or 供应商 selects its first model in menu order with that model's own budget, unless the model is
        among its models already; a model starts from its own budget; a budget must be the model's. The same choice
        as before changes nothing."""
        if not isinstance(option, str):
            return '⚠️ 没有收到选择，请重新选择。'
        settings = self.proxy.settings(ctx)
        choices = available_models(self.proxy.topics, ctx)
        current = self.current(settings, choices)
        key, budget = current or ('', '')
        harness = harness_of(key, choices[key]) if current else settings.get('harness') or ''
        if kind == HARNESS:
            if option not in tiers(choices, option)[0]:
                return '⚠️ 这个 Harness 已不在可选目录中，请重新选择。'
            key = tier_pick(choices, key, harness=option) or key
        elif kind == PROVIDER:
            if not harness or option not in tiers(choices, harness)[1]:
                return '⚠️ 这个供应商已不在可选目录中，请重新选择。'
            key = tier_pick(choices, key, harness=harness, provider=option) or key
        elif kind == TIER_MODEL:
            key = f'{harness}:{option}'
            if key not in choices:
                return '⚠️ 这个模型已不在可选目录中，请重新选择。'
        else:
            if not current:
                return '⚠️ 当前模型不在可选目录中，请先选择模型。'
            if option not in dict(effort_options(choices[key])[0]):
                return '⚠️ 这个模型没有这一档思考预算，请重新选择。'
            budget = option
        if current and (key, budget) == current:
            return '代答话题模型没有变。'
        if kind != TIER_BUDGET:
            budget = ''  # Another model: its own default.
        return self.save(ctx, choices, key, budget)

    def choose(self, ctx, kind, option):
        """A model or budget picked in a select of a card drawn before the rows: the settings bound to new contact
        topics."""
        proxy = self.proxy
        if not isinstance(option, str):
            return '⚠️ 没有收到选择，请重新选择。'
        choices = available_models(proxy.topics, ctx)
        if kind == MODEL:
            if option not in choices:
                return '⚠️ 这个模型已不在可选目录中，请重新选择。'
            key, budget = option, ''
        else:
            current = self.current(proxy.settings(ctx), choices)
            if current is None:
                return '⚠️ 当前模型不在可选目录中，请先选择模型。'
            key, budget = current[0], option
            if budget not in dict(effort_options(choices[key])[0]):
                return '⚠️ 这个模型没有这一档思考预算，请重新选择。'
        return self.save(ctx, choices, key, budget)

    def save(self, ctx, choices, key, budget):
        """The menu entry ``key`` with the budget option ``budget`` ('' its own default) as the settings bound to new
        contact topics; Proxy.sync_topics moves the idle ones over (set_model marks the change)."""
        choice = choices[key]
        if not budget:
            options, default = effort_options(choice)
            budget = default or options[0][0]
        model, effort = run_choice(choice, budget_effort(choice, budget))
        self.proxy.commands.set_model(ctx, choice['harness'], model, effort)
        return f"✓ 代答话题模型：{choice['label']} / {effort or '模型默认'}。已有联系人话题空闲时也会换过去。"

    # After the click -------------------------------------------------------------------------

    def enable(self, ctx):
        return {'notice': '✓ ' + self.proxy.commands.cmd_enable(ctx, '', '', '')}

    def find(self, ctx, query):
        entries, left = records.first_names(records.parse_names(query))
        items = self.proxy.commands.lookup(self.proxy.settings(ctx)['user_profile'], entries) if entries else []
        return {'lookup': {'id': uuid.uuid4().hex[:8], 'text': query, 'items': items, 'left': left}, 'found': None,
                'spent': None, 'notice': '' if items else '⚠️ 输入框里没有可以查找的名字。'}

    def members(self, ctx, chat_id, name):
        """The group's members as a confirmation on the card, or why there is nothing to confirm."""
        proxy = self.proxy
        settings = proxy.settings(ctx)
        users, total, bots, _ = proxy.commands.group_members(settings['user_profile'], chat_id)
        if total > records.MEMBER_LIMIT:
            return {'members': None, 'notice': f'⚠️ 群「{name}」有 {total} 人，超过一次最多加入的 {records.MEMBER_LIMIT} 人，'
                                               '没有添加。可以在查找框里写要加的人。'}
        add = records.new_members(users, settings.get('owner_user_id') or '', records.read_contacts(ctx.state_dir))
        if not add:
            return {'members': None, 'notice': f'⚠️ 群「{name}」里没有可以加入的人：共 {total} 人，'
                                               f'除去你自己和已在白名单的 {len(users)} 人后为 0'
                                               + (f'（机器人 {bots} 个不加）' if bots else '') + '。'}
        return {'notice': '', 'members': {'id': uuid.uuid4().hex[:8], 'chat_id': chat_id, 'name': name, 'total': total,
                                          'bots': bots, 'skipped': len(users) - len(add), 'add': add,
                                          'partial': len(users) < total}}

    def add_members(self, ctx, members):
        proxy = self.proxy
        settings = proxy.settings(ctx)
        added, listed = proxy.commands.add_contacts(
            ctx, settings, [{'kind': 'p2p', 'target': m['open_id'], 'name': m['name'] or m['open_id'], 'chat_id': ''}
                            for m in members['add']], source={'chat_id': members['chat_id'], 'name': members['name']})
        return {'added': len(added), 'notice': f"✓ 已把群「{members['name']}」的 {len(added)} 人加入白名单，私聊代答，每人一个话题"
                          + (f'（{len(listed)} 人已在白名单）' if listed else '')
                          + '。之后进群的人不会自动加入，可点白名单里这个群的“同步成员”补加。'
                          + ('' if settings.get('enabled') else '代答目前是关闭的，开启后生效。')}

    def finish(self, ctx, path, kind, work):
        """Run a slow click's work while its busy card is drawn, then draw its result (or its error)."""
        shown = threading.Thread(target=timing.carry(functools.partial(self.redraw, ctx, path)),
                                 name='proxy-panel-busy', daemon=True)
        shown.start()
        try:
            changes = work()
        except FAILURES as exc:
            changes = {'notice': f'⚠️ {action_label(kind)}未完成：{exc}'}
        except Exception as exc:  # noqa: BLE001 - the card must not stay busy
            self.proxy.runtime.log(f'proxy panel {kind} failed: {type(exc).__name__}')
            changes = {'notice': f'⚠️ {action_label(kind)}未完成：{type(exc).__name__}'}
        finally:
            shown.join()  # The result's sequence comes after the busy card's.
        later = None
        with self.menu.lock(path):
            notes = self.working.get(path.stem) or []
            if notes:
                notes.pop(0)
            if not notes:
                self.working.pop(path.stem, None)
            try:
                record = read_json(path)
            except (OSError, ValueError):
                return
            if not self.menu.shows(record):
                return
            added = changes.pop('added', 0)
            view = {**record['view'], **changes}
            view['revision'] += 1
            if added:
                self.spend(view)  # Like any add: the search the group came from leaves the card.
            if not self.publish(ctx, path, record, view):
                later = record, view, changes.get('notice') or ''
        if later:
            self.unsaved(ctx, *later)
