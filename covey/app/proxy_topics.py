"""Proxy replies (代答): each contact's topic in the proxy group. Opened once and bound like a fork (no launch
card), found again for a contact added back (``ensure_topic``), routed like any message there
(``topic_context``), and moved to the model chosen on the 代答 card the way 换配置 does (``sync_topics``).

Held by Proxy as ``contact_topics``. Contacts are written under Proxy.lock and topic.json under LOCK, the
Feishu calls outside both (rule 1).
"""
from __future__ import annotations

import dataclasses
import time
from pathlib import Path

from covey.app.binding import switch_topic
from covey.cards.elements import notice_card
from covey.core import proxy as records
from covey.core.locks import LOCK
from covey.core.state import read_object, write_json
from covey.core.topics import context_for_topic, read_topic


class ProxyTopics:
    """The contacts' topics; no state of its own."""

    def __init__(self, proxy) -> None:
        self.proxy = proxy

    def sync_topics(self, ctx, settings):
        """Move each contact topic that has not caught up with the last model change on the card to the
        settings' Harness, model and effort, the way 换配置 does (app/binding.py ``switch_topic``:
        a fresh session that is handed the conversation). One model for all of 代答.
        A topic with a task or a batch under way waits for a later poll; a topic switched by
        hand after the change keeps its own choice."""
        changed = float(settings.get("model_changed_at") or 0)
        if not changed:
            return
        busy = {b["contact"] for b in records.read_batches(ctx.state_dir) if b["status"] in {"submitted", "sending"}}
        for contact in records.read_contacts(ctx.state_dir):
            if float(contact.get("model_synced_at") or 0) >= changed or not contact.get("topic_id") \
                    or not contact.get("root_id") or contact["key"] in busy:
                continue
            want = (settings.get("harness"), settings.get("model", ""), settings.get("effort", ""))
            _, _, target = self.topic_context(ctx, settings, contact, contact["root_id"])
            topic = read_topic(target)
            if (topic.get("harness_id"), topic.get("model", ""), topic.get("effort", "")) != want:
                if want[0] not in self.proxy.topics.apps:
                    continue
                form = {"harness_id": topic.get("harness_id"), "binding_revision": topic.get("binding_revision", 1),
                        "job_id": None}
                try:
                    switch_topic(self.proxy.topics, target, settings["group_chat_id"], form, *want)
                except ValueError as exc:  # Busy, or changed meanwhile: a later poll tries again.
                    self.proxy.runtime.log(f"proxy topic={target.topic_id[:12]} model sync waits: {exc}")
                    continue
                self.proxy.messenger.send_message(
                    ctx.profile, settings["group_chat_id"],
                    f"🔁 代答模型已换成 {want[0]} / {want[1] or '默认模型'} / {want[2] or '模型默认'}（代答卡上改的），"
                    "本话题之后由它接着回，之前的对话会交给它。",
                    self.proxy.voice.key("proxy-model-sync", f"{target.topic_id[:16]}-{int(changed)}"),
                    reply_to_message_id=contact["root_id"], voice=self.proxy.voice)
            with self.proxy.lock:
                current = next((c for c in records.read_contacts(ctx.state_dir) if c["key"] == contact["key"]), None)
                if current is not None:
                    current["model_synced_at"] = time.time()
                    records.write_contact(ctx.state_dir, current)

    def app_for(self, settings):
        app = self.proxy.topics.apps.get(settings.get("harness") or "")
        if app is None:
            raise ValueError(f"代答设置的 Harness 不可用：{settings.get('harness')}")
        return app

    def ensure_topic(self, ctx, settings, contact):
        """The contact's topic in the proxy group, bound like a fork (no launch card): created once, and
        found again for a contact added back (one contact, one topic, one session; its binding stays).
        None when the contact left the whitelist meanwhile (it is not written back)."""
        if contact.get("topic_id") and contact.get("root_id"):
            return contact
        group = settings["group_chat_id"]
        root = self.earlier_root(ctx, group, contact["key"])
        if root:
            target, opened = self.topic_context(ctx, settings, {**contact, "root_id": root}, root)[2], "found again"
        else:
            app = self.app_for(settings)
            text = (f"**代答 · {contact['name']}**（{'私聊' if contact['kind'] == 'p2p' else '群里 @你的消息'}）\n"
                    "对方的消息和代你发出的回复都记在本话题。想改怎么回，在本话题 @机器人 告诉我，"
                    "我会按你的意思回复对方。")
            # Keyed on this time on the whitelist: a key once used never carries another card
            # (feishu/transport.py refuses it), and a contact added back may have had a topic.
            key = f"{records.digest(contact['key'])}-{int(float(contact.get('added_at') or 0))}"
            _, root = self.proxy.messenger.create_and_send_card(ctx.profile, group, notice_card(text, "代答"),
                                                                 self.proxy.voice.key("proxy-root", key), voice=self.proxy.voice)
            if not root:
                raise RuntimeError("代答话题的起始消息发送失败")
            base = dataclasses.replace(app.harness.context, workspace_name=settings["workspace"],
                                       workdir=Path(settings["workdir"]))
            target = context_for_topic(base, {"chat_id": group, "message_id": root, "root_id": root},
                                       app.harness.store.sessions_dir)
            with LOCK:
                data = read_topic(target)
                data.update(harness_id=app.harness.spec.name, model=settings.get("model", ""),
                            effort=settings.get("effort", ""), model_settings_snapshot=True,
                            workspace_name=settings["workspace"], workdir=settings["workdir"], binding_revision=1,
                            workspace_locked=True, harness_locked=True,
                            proxy={"contact": contact["key"], "chat_id": group, "root_id": root})
                write_json(target.history_path.parent / "topic.json", data, ensure_ascii=False, indent=2)
            opened = "opened"
        with self.proxy.lock:
            current = next((c for c in records.read_contacts(ctx.state_dir) if c["key"] == contact["key"]), None)
            if current is not None:
                current.update(root_id=root, topic_id=target.topic_id)
                records.write_contact(ctx.state_dir, current)
        self.proxy.runtime.log(f"proxy topic {opened} topic={target.topic_id[:12]} contact={records.digest(contact['key'])[:8]}")
        return current

    @staticmethod
    def earlier_root(ctx, group, key):
        """The root message of the topic contact ``key`` had in proxy group ``group`` before it left the
        whitelist (the newest, by its topic.json), '' when it never had one."""
        found = []
        for path in (ctx.state_dir / "topics").glob("*/topic.json"):
            data = read_object(path)
            proxy = data.get("proxy")
            root = isinstance(proxy, dict) and proxy.get("contact") == key and (proxy.get("root_id") or data.get("root_id"))
            if root and (proxy.get("chat_id") or data.get("chat_id")) == group:
                found.append((int(data.get("topic_number") or 0), path.stat().st_mtime, root))
        return max(found)[2] if found else ""

    def topic_context(self, ctx, settings, contact, message_id):
        """(app, event, context) of a message in the contact's topic: the Harness the topic is bound to,
        as for any message there (a model chosen later binds only new topics)."""
        event = {"chat_id": settings["group_chat_id"], "message_id": message_id, "root_id": contact["root_id"],
                 "chat_type": "group"}
        app, target = self.proxy.topics.route(ctx, event)
        return app, event, target
