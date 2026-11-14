"""Proxy replies (代答): what the 代答设置 card (app/proxy_panel.py) asks for, its settings, and finding
the contacts to whitelist.

- The card opens from the bot menu (app/bot_menu.py); there are no 代答 text commands (the owner,
  2026-11-03: typed commands misfired), so 代答 … is an ordinary message. 开启代答 creates the proxy
  group the first time and fixes the Harness, model and workspace new topics are bound to.
- Adding: one 查找 on the card takes several names, e-mails or IDs (core/proxy.py parse_names)
  and looks them all up at once (``lookup``: every lark-cli search in parallel); the card can also
  add every member of a group.

Held by Proxy as ``commands``; settings and contacts change under Proxy.lock, through core/proxy.py.
"""
from __future__ import annotations

import functools
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from covey.app.proxy_send import user_cli
from covey.core import extensions
from covey.core import proxy as records
from covey.core import timing
from covey.core.state import read_json
from covey.core.workspaces import load_config
from covey.feishu.transport import decode, lark_argv, last_json, run_cli

MEMBER_PAGES = 6  # Pages of 100 members: past MEMBER_LIMIT, so a larger group is told apart.
LOOKUPS = 8  # lark-cli calls one search runs at the same time.
CHOICES, CHOICES_EACH = 8, 4  # Candidates listed for a name looked up alone, and for each of several.
EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def cli_error(proc):
    """What went wrong with a lark-cli call, in a line: its error message when it printed one, else its
    first line of stderr, else its exit code; never a tail of JSON."""
    for text in (proc.stderr, proc.stdout):
        error = last_json(text).get("error")
        message = error.get("message") if isinstance(error, dict) else error
        if isinstance(message, str) and message.strip():
            return " ".join(message.split())[:160]
    line = next((line.strip() for line in (proc.stderr or "").splitlines()
                 if line.strip() and not line.lstrip().startswith(("{", "}", '"'))), "")
    return line[:160] or (f"lark-cli 退出码 {proc.returncode}" if proc.returncode else "lark-cli 没有返回群 ID")


def parallel(work):
    """Each zero-argument call of ``work`` run at the same time (LOOKUPS at most), as part of this
    thread's flow: its result, or the exception it raised, in order."""
    if len(work) < 2:
        return [_outcome(call) for call in work]
    with ThreadPoolExecutor(max_workers=min(LOOKUPS, len(work)), thread_name_prefix="proxy-lookup") as pool:
        futures = [pool.submit(timing.carry(call)) for call in work]
    return [future.exception() or future.result() for future in futures]


def _outcome(call):
    try:
        return call()
    except Exception as exc:  # noqa: BLE001 - handed back as the pool hands it
        return exc


def number(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def person(user):
    """A ``contact +search-user`` user as a candidate to add."""
    return {"kind": "p2p", "target": user["open_id"], "key": records.contact_key("p2p", user["open_id"]),
            "name": user.get("localized_name") or "", "chat_id": user.get("p2p_chat_id") or "",
            "detail": user.get("department") or "人", "email": user.get("email") or ""}


def group(chat_id, name, members=None):
    """A group as a candidate to add; ``members``: how many people it has, when known."""
    found = {"kind": "group", "target": chat_id, "key": records.contact_key("group", chat_id), "name": name,
             "chat_id": chat_id, "detail": "群", "email": ""}
    if members is not None:
        found["members"] = members
    return found


def search_people(profile, query):
    args = ("--user-ids", query) if query.startswith("ou_") else ("--query", query)
    users = user_cli(profile, "contact", "+search-user", *args).get("data", {}).get("users") or []
    return [person(u) for u in users if u.get("open_id")]


def chat_info(profile, chat_id):
    return user_cli(profile, "api", "GET", f"/open-apis/im/v1/chats/{chat_id}").get("data") or {}


def search_groups(profile, query):
    if query.startswith("oc_"):
        chat = chat_info(profile, query)
        return [group(query, chat["name"], number(chat.get("user_count")))] if chat.get("name") else []
    chats = user_cli(profile, "im", "+chat-search", "--query", query).get("data", {}).get("chats") or []
    return [group(g["chat_id"], g.get("name") or "") for g in chats if g.get("chat_id")]


def exact(candidates, query):
    """The candidates ``query`` names exactly: by name, e-mail or ID."""
    return [c for c in candidates if query in {c["name"], c["email"], c["target"]}]


class ProxyCommands:
    """代答's settings and its whitelist: what the owner asks for on the 代答设置 card (app/proxy_panel.py
    calls these)."""

    def __init__(self, proxy) -> None:
        self.proxy = proxy

    @staticmethod
    def sources(contacts):
        """{group chat_id: the contacts added as its members}, in the order they were added."""
        found = {}
        for contact in contacts:
            if records.source_of(contact):
                found.setdefault(records.source_of(contact), []).append(contact)
        return found

    def cmd_enable(self, ctx, _argument, chat_id, _message):
        settings = self.proxy.settings(ctx)
        if not settings["user_profile"]:
            raise ValueError("未配置 COVEY_PROXY_PROFILE（带你本人登录的 lark-cli profile）")
        me = (user_cli(settings["user_profile"], "contact", "+search-user", "--user-ids", "me")
              .get("data", {}).get("users") or [{}])[0]
        if not me.get("open_id"):
            raise RuntimeError("读不到 profile 里的登录用户")
        found = {"owner_user_id": me["open_id"], "owner_name": me.get("localized_name") or ""}
        if not settings.get("harness"):
            found.update(self.latest_binding(ctx))
        if not settings.get("workdir"):
            config = load_config(ctx.state_dir, ctx.default_workdir)
            found.update(workspace=config["default"], workdir=str(config["workspaces"][config["default"]]))
        if not settings.get("group_chat_id"):
            found.update(group_chat_id=self.create_group(ctx, settings), group_name=records.GROUP_NAME)
        with self.proxy.lock:
            # Read again: a model chosen while the calls above ran is kept.
            settings = self.proxy.settings(ctx)
            settings.update({k: v for k, v in found.items() if k.startswith("owner_") or not settings.get(k)},
                            enabled=True)
            settings.pop("group_creating", None)  # The group is known now.
            records.write_settings(ctx.state_dir, settings)
        return (f"代答已开启：对方的消息和代发的回复记在群「{settings.get('group_name') or records.GROUP_NAME}」，每个联系人一个话题。\n"
                f"代答话题用 {settings['harness']} / {settings.get('model') or '默认模型'} / "
                f"{settings.get('effort') or '模型默认'}，工作区 {settings['workspace']}。")

    def latest_binding(self, ctx):
        """Harness, model and effort of the most recently bound topic: what the owner works with now."""
        newest, binding = 0.0, {}
        for path in (ctx.state_dir / "topics").glob("*/topic.json"):
            try:
                mtime, data = path.stat().st_mtime, read_json(path)
            except (OSError, ValueError):
                continue
            if mtime > newest and data.get("harness_id") in self.proxy.topics.apps and data.get("model_settings_snapshot"):
                newest, binding = mtime, {"harness": data["harness_id"], "model": data.get("model", ""),
                                          "effort": data.get("effort", "")}
        if not binding:
            raise ValueError("还没有可沿用的模型配置，请先在代答卡上选择模型")
        return binding

    def create_group(self, ctx, settings):
        """The proxy group's chat_id. The try is recorded first (``group_creating``, under Proxy.lock), and
        Feishu asked after it (hard rule 1): once a try's outcome is unknown, the group is looked up by its
        name among the owner's chats and never created a second time; one Feishu refused is tried again."""
        owner = self.proxy.runtime.owner_open_id or ""
        if not owner:
            raise RuntimeError("机器人还没有主人，不能建群")
        with self.proxy.lock:
            current = records.read_settings(ctx.state_dir)
            if current.get("group_chat_id"):
                return current["group_chat_id"]
            tried = current.get("group_creating")
            if not tried:
                current["group_creating"] = {"at": time.time()}
                records.write_settings(ctx.state_dir, current)
        if tried:
            return self.unknown_group(settings)
        try:
            proc = run_cli(lark_argv(ctx.profile, "im", "+chat-create", "--as", "bot", "--name", records.GROUP_NAME,
                                     "--users", owner, "--owner", owner, "--description",
                                     "代答：白名单联系人的消息和代你发出的回复，每个联系人一个话题", "--format", "json"),
                           runner=subprocess.run, capture_output=True, text=True, timeout=60, check=False)
        except subprocess.TimeoutExpired as exc:
            self.proxy.runtime.log(f"proxy group creation unknown: {type(exc).__name__}")
            raise RuntimeError(f"建群没有确认结果（{type(exc).__name__}）；再点开启代答时按群名找「{records.GROUP_NAME}」，"
                               "不会再建一个") from None
        except OSError as exc:  # lark-cli never started (not found, no fork): no group, the next click tries again.
            self.proxy.runtime.log(f"proxy group not created: {type(exc).__name__}")
            self.forget_group_try(ctx)
            raise RuntimeError(f"建群失败：{type(exc).__name__}") from None
        chat_id = (last_json(proc.stdout).get("data") or {}).get("chat_id") if proc.returncode == 0 else ""
        if not chat_id:
            # The owner read 10-27's raw tail of JSON as the reply (repo review U10): the whole output is logged.
            self.proxy.runtime.log(f"proxy group not created rc={proc.returncode}: {(proc.stderr or proc.stdout or '').strip()[-500:]}")
            _, failure = decode(proc)
            if failure is not None and not failure.ambiguous:  # Feishu refused: no group, the next click tries again.
                self.forget_group_try(ctx)
            raise RuntimeError(f"建群失败：{cli_error(proc)}")
        self.proxy.runtime.log(f"proxy group created chat={chat_id[-10:]}")
        return chat_id

    def forget_group_try(self, ctx):
        """A try known to have made no group: the next 开启代答 creates it again."""
        with self.proxy.lock:
            current = records.read_settings(ctx.state_dir)
            current.pop("group_creating", None)
            records.write_settings(ctx.state_dir, current)

    def unknown_group(self, settings):
        """The group an earlier try of unknown outcome made: the owner's one chat of that name."""
        named = [g for g in search_groups(settings["user_profile"], records.GROUP_NAME)
                 if g["name"] == records.GROUP_NAME]
        if len(named) == 1:
            self.proxy.runtime.log(f"proxy group found by name chat={named[0]['chat_id'][-10:]}")
            return named[0]["chat_id"]
        if named:
            raise RuntimeError(f"上次建群没有确认结果，按群名找到 {len(named)} 个「{records.GROUP_NAME}」，不知道用哪个："
                               "只留一个后再点开启代答")
        raise RuntimeError(f"上次建群没有确认结果，按群名也没找到「{records.GROUP_NAME}」，为免建出第二个群没有再建："
                           "刚建的群可能稍后才搜得到，过几分钟再点开启代答；确实没有的话，自己建一个同名群并把机器人加进去")

    def cmd_disable(self, ctx, _argument, _chat, _message):
        with self.proxy.lock:
            settings = records.read_settings(ctx.state_dir)
            settings["enabled"] = False
            records.write_settings(ctx.state_dir, settings)
        return "代答已关闭：不再读取和回复白名单消息；正在进行的任务照常结束，但结果不再发给对方。"

    def set_model(self, ctx, harness, model, effort):
        """The Harness, model and effort of every contact topic: new ones are bound to it, and the
        worker moves existing ones over once idle (``sync_topics``)."""
        with self.proxy.lock:
            settings = records.read_settings(ctx.state_dir)
            if (settings.get("harness"), settings.get("model"), settings.get("effort")) != (harness, model, effort):
                settings["model_changed_at"] = time.time()
            settings.update(harness=harness, model=model, effort=effort)
            records.write_settings(ctx.state_dir, settings)

    def find(self, profile, queries):
        """({query: its candidates, exact matches first}, {query: the error of a query found nowhere
        because its searches failed}): every query's people and groups searched as the owner, all
        at once. When every search failed, the first error is raised."""
        work, asked = [], []
        for query in queries:
            if not query.startswith("oc_"):
                work.append(functools.partial(search_people, profile, query))
                asked.append(query)
            if not query.startswith("ou_") and not EMAIL.fullmatch(query):
                work.append(functools.partial(search_groups, profile, query))
                asked.append(query)
        results = parallel(work)
        failures = [r for r in results if isinstance(r, Exception)]
        if failures and len(failures) == len(results):
            raise failures[0]
        found, failed = {query: [] for query in queries}, {}
        for query, result in zip(asked, results):
            if isinstance(result, Exception):
                failed.setdefault(query, result)
            else:
                found[query] += result
        return ({query: sorted(found[query], key=lambda c, q=query: q not in {c["name"], c["email"], c["target"]})
                 for query in queries}, {query: error for query, error in failed.items() if not found[query]})

    def lookup(self, profile, entries, names=None):
        """Each name of ``entries`` (core/proxy.py parse_names) looked up, all at once: one item per
        name, in order, {query, state, candidates[, more, error]}. ``state`` is unique (one exact
        match: the one to add), choices (several exact matches, or none but similar ones), none or
        error. A phrase with an exact match is one item, else each of its words is. Groups listed
        come with their member count. ``names``: what a word is shown as (an @ looked up by its ID)."""
        names = names or {}
        queries = list(dict.fromkeys([phrase for phrase, _ in entries if phrase] + [w for _, words in entries for w in words]))
        found, errors = self.find(profile, queries)
        wanted = [query for phrase, words in entries
                  for query in ([phrase] if phrase and exact(found[phrase], phrase) else words)]
        limit = CHOICES if len(wanted) == 1 else CHOICES_EACH
        items = []
        for query in wanted:
            item = {"query": names.get(query, query), "state": "none", "candidates": []}
            matches = exact(found[query], query)
            shown = matches or found[query]
            if len(matches) == 1:
                item.update(state="unique", candidates=matches)
            elif shown:
                item.update(state="choices", candidates=shown[:limit], more=max(0, len(shown) - limit))
            elif query in errors:
                item.update(state="error", error=" ".join(str(errors[query]).split())[:200])
            items.append(item)
        counted = list(dict.fromkeys(c["target"] for item in items for c in item["candidates"]
                                     if c["kind"] == "group" and "members" not in c))
        sizes = dict(zip(counted, parallel([functools.partial(chat_info, profile, chat) for chat in counted])))
        for candidate in (c for item in items for c in item["candidates"] if c["target"] in sizes):
            size = sizes[candidate["target"]]
            if isinstance(size, dict) and number(size.get("user_count")) is not None:
                candidate["members"] = number(size["user_count"])
        return items

    def add_contact(self, ctx, settings, found):
        """``found`` (a candidate of ``lookup``) into the whitelist; what to tell the owner."""
        added, listed = self.add_contacts(ctx, settings, [found])
        if listed:
            return f"{listed[0]['name']} 已在白名单。"
        return (f"已加入白名单：{found['name']}（{'私聊' if found['kind'] == 'p2p' else '群里 @你的消息'}）。"
                "从现在起的新消息会自动代答" + ("。" if settings.get("enabled") else "；代答目前是关闭的，开启后生效。"))

    def add_contacts(self, ctx, settings, founds, *, source=None):
        """Each of ``founds`` (candidates of ``lookup``) into the whitelist unless it is there already:
        (the contacts added, those already there), in order. ``source``: the group they are members of."""
        added, listed = [], []
        with self.proxy.lock:
            existing = {c["key"]: c for c in records.read_contacts(ctx.state_dir)}
            for found in founds:
                key = records.contact_key(found["kind"], found["target"])
                if key in existing:
                    if existing[key] not in listed:
                        listed.append(existing[key])
                    continue
                now = time.time()
                contact = {"key": key, "kind": found["kind"], "target": found["target"], "name": found["name"],
                           "chat_id": found.get("chat_id", ""), "paused": False, "since": now, "added_at": now,
                           "seen": [], **({"source": source} if source else {})}
                contact["session_dir"] = records.notes_dir(Path(settings.get("workdir") or ctx.default_workdir), contact,
                                                           time.strftime("%Y%m%d"))
                records.write_contact(ctx.state_dir, contact)
                existing[key] = contact
                added.append(contact)
        return added, listed

    def group_members(self, profile, chat_id):
        """The people of group ``chat_id``, read as the owner: ([{open_id, name}], how many people it
        has, how many bots, whether the list is whole)."""
        data = user_cli(profile, "im", "+chat-members-list", "--chat-id", chat_id, "--page-all", "--page-size", "100",
                        "--page-limit", str(MEMBER_PAGES), "--page-delay", "0", timeout=180).get("data") or {}
        users = [{"open_id": str(u["member_id"]), "name": str(u.get("name") or "")} for u in data.get("users") or []
                 if isinstance(u, dict) and str(u.get("member_id") or "").startswith("ou_")]
        total = number(data.get("user_total"))
        return (users, len(users) if total is None else total, len(data.get("bots") or []),
                not data.get("has_more") and not data.get("truncations"))

    def find_contact(self, ctx, argument):
        contacts = records.read_contacts(ctx.state_dir)
        matches = [c for c in contacts if argument in {c["name"], c["target"], c["key"]}]
        if len(matches) > 1:
            raise ValueError(f"白名单里有多个“{argument}”，请用 ID：" + "、".join(c["target"] for c in matches))
        if not matches:
            raise ValueError(f"白名单里没有“{argument}”，可以用名称或 ou_/oc_ ID")
        return matches[0]

    def set_source_paused(self, ctx, chat_id, paused):
        """Pause or resume every member added with group ``chat_id``: (the group's name, how many
        members it has, how many changed); a resumed one answers only what comes after."""
        with self.proxy.lock:
            members = [c for c in records.read_contacts(ctx.state_dir) if records.source_of(c) == chat_id]
            changed = [c for c in members if bool(c.get("paused")) != paused]
            for contact in changed:
                contact.update(paused=paused, **({} if paused else {"since": time.time()}))
                records.write_contact(ctx.state_dir, contact)
        return (members[0]["source"].get("name") or chat_id if members else ""), len(members), len(changed)

    def remove_source(self, ctx, chat_id):
        """Every member added with group ``chat_id`` out of the whitelist: (the group's name, how many)."""
        with self.proxy.lock:
            members = [c for c in records.read_contacts(ctx.state_dir) if records.source_of(c) == chat_id]
            for contact in members:
                records.remove_contact(ctx.state_dir, contact["key"])
        return (members[0]["source"].get("name") or chat_id if members else ""), len(members)

    def cmd_remove(self, ctx, argument, _chat, _message):
        with self.proxy.lock:
            contact = self.find_contact(ctx, argument)
            records.remove_contact(ctx.state_dir, contact["key"])
        return f"已移出白名单：{contact['name']}。它的话题保留，之后的消息不再代答。"

    def set_paused(self, ctx, argument, paused):
        with self.proxy.lock:
            contact = self.find_contact(ctx, argument)
            contact.update(paused=paused, **({} if paused else {"since": time.time()}))
            records.write_contact(ctx.state_dir, contact)
        return f"{contact['name']}：{'已暂停代答' if paused else '已恢复代答（只回恢复之后的新消息）'}。"

    def cmd_login(self, ctx, _argument, chat_id, message_id, *, key=""):
        """Start the send tool's QR login, its messages replying to ``message_id`` (the panel's: under its card);
        ``key`` tells one login's messages from another's under the same message."""
        settings = self.proxy.settings(ctx)
        hook = extensions.hook("proxy_sender")
        problem = hook.send_problem(settings) if hook else "没有代答发送器（没有启用提供它的扩展）"
        if problem:
            raise ValueError(problem)
        if not settings["user_profile"]:
            raise ValueError("未配置 COVEY_PROXY_PROFILE（带你本人登录的 lark-cli profile）")
        if not self.proxy.web_lock.acquire(blocking=False):
            return "代答发送工具正在使用（发送或登录中），稍后再试。"
        threading.Thread(target=self.proxy.web_login.login, args=(ctx, settings, chat_id, message_id, key or message_id),
                         name="proxy-login", daemon=True).start()
        return ("正在生成登录二维码，马上发到这里，请用飞书手机端扫码（3 分钟内有效；用你本人的账号，"
                "和读消息的 lark-cli 用户是同一人）。")
