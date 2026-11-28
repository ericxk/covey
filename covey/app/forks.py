"""Forks: a new topic that continues from another topic, opened by the butler's topic_fork
(app/butler_topics.py). The 🔀 分叉 and 💬 侧聊 buttons that opened them from a task card are gone.

A 话题区 fork hangs under the owner's question. Without one
(the question already has a topic, or it is not known) the source topic's thread is forwarded into the chat as a
merged message, which becomes the new topic's root and shows the original conversation folded. Feishu refuses to
forward a long thread (a 60-message topic was refused); the new topic then starts from a short root card
instead. The bridge replies under the root with a short header and binds the new topic to the source's Harness,
model and workspace, so the first reply runs without a launch card.

That first run forks the source's native session. While the source topic is running, or on a backend without native
fork, it starts from the source's completed conversation record instead. Topics forked before keep their
``forked_from``; one opened as a 侧聊 is an ordinary topic from then on.
"""
from __future__ import annotations

import dataclasses
import hashlib
import time
from pathlib import Path

from covey.core.defaults import work_mode_tag
from covey.core.harness import backend_capabilities
from covey.core.locks import LOCK, resource_lock
from covey.core.state import read_json, write_json
from covey.core.history_storage import recent_history
from covey.core.topics import claim_area_root, context_for_topic, mark_home, read_topic, topic_label
from covey.cards.elements import notice_card
from covey.core.store import directory_store
from covey.core.text import cut

TITLE = "🔀 分叉"
FINISHED = frozenset({"done", "waiting_user", "continued"})  # A turn a fork may start from.
# A thread Feishu will not forward whole (230001 without saying why, seen on a 19-message
# topic and a 50-plus one) goes as a merged forward of its newest messages, those a merge takes: no system messages,
# no merged forwards (a fork's own root; the session carries it). If that is refused too, only the people's messages.
THREAD_PAGE = 50


def home_fields(source, now=None):
    """What a fork takes from the source's ``topic.json`` about the home: ``home_start`` (with a start of its own, ``now``, and
    the source's ``slug``: core/topics.py mark_home), and the repos it touched as read-only ones (a fork never carries on in the
    source's worktree: ``worktree``, ``branch``, ``base`` and ``base_oid`` go, ``source`` becomes ``touched``). Nothing for a
    topic that has neither."""
    fields = {}
    if source.get("home_start") is True:
        mark_home(fields, title=str(source.get("slug") or ""), now=now)
    elif "home_start" in source:
        fields["home_start"] = source["home_start"]
    repos = source.get("repos")
    if isinstance(repos, list):
        dropped = ("worktree", "branch", "base", "base_oid")
        fields["repos"] = [{**{k: v for k, v in repo.items() if k not in dropped}, "mode": "read", "source": "touched"}
                           for repo in repos if isinstance(repo, dict)]
    return fields


def fork_token(topic_id, click_id):
    """One fork per request: the new topic's key in the source topic's ``forks``."""
    return hashlib.sha256(f"{topic_id}\0{click_id}".encode()).hexdigest()[:24]


class Forks:
    """One Harness's forks: opening the new topic and its first run.

    - ``harness``: the source topic's Harness (its session store and native-fork capability).
    - ``runtime``: the job registry (is the source running here?) and the log.
    - ``messenger``: the forwarded thread, the root card and the header reply.
    - ``cards``: the source's model as the task cards name it.
    - ``sessions``: the source topic's native session pointer.
    """

    def __init__(self, harness, *, runtime, messenger, cards, sessions) -> None:
        self.harness = harness
        self.runtime = runtime
        self.messenger = messenger
        self.voice = harness.voice
        self.cards = cards
        self.sessions = sessions

    def create_fork(self, ctx, chat, record, click_id, *, area=""):
        """Open the new topic once per ``click_id`` (the butler's request, app/butler_topics.py topic_fork);
        returns this source topic's forks. ``area``:
        the owner's question a 话题区 fork hangs under (app/butler_topics.py place): no forwarded thread, no root
        card; the question becomes the fork's root and its header goes in the question's thread."""
        if record.get("status") not in FINISHED:
            raise ValueError("本轮完成后才能分叉")
        token = fork_token(ctx.topic_id, click_id)
        source_path = ctx.history_path.parent / "topic.json"
        with resource_lock("fork", token):
            source = read_topic(ctx)
            done = [f for f in source.get("forks", []) if f.get("token") == token]
            if done:
                return source.get("forks", [])
            from covey.feishu.message_read import message_item, fetch_message_detail
            source_label = topic_label(ctx.state_dir, ctx.topic_id)
            root, forwarded = area, False
            if area:  # The butler's message until now: from now on the fork's (its context makes the topic).
                claim_area_root(ctx.state_dir, chat, area)
            if not area:
                item = message_item(fetch_message_detail(source["root_id"], ctx.profile), source["root_id"]) or {}
                root = self.forward_thread(ctx, chat, item["thread_id"], token) if item.get("thread_id") else ""
                forwarded = bool(root)
            if not root:
                self.runtime.log(f"fork thread not forwarded topic={ctx.topic_id[:12]}; starting from a root card")
                root = self.fork_root_card(ctx, chat, record, token, source_label)
            if not root:
                raise ValueError("新话题的起始消息发送失败，未创建新话题；可稍后再点")
            target = context_for_topic(ctx, {"chat_id": chat, "message_id": root, "root_id": root},
                                       self.harness.store.sessions_dir)
            # The session is the source's on its Harness then: harness_id says which, so no other Harness is handed it.
            origin = {"topic_id": ctx.topic_id, "job_id": record["job_id"], "mode": "fork", "at": time.time(),
                      "session_id": self.sessions.read_session_id(ctx.session_path) or "",
                      "harness_id": source.get("harness_id") or self.harness.spec.name,
                      "history_path": str(ctx.history_path)}
            with LOCK:
                data = read_topic(target)
                data.update({key: source[key] for key in ("harness_id", "model", "effort", "model_settings_snapshot",
                                                          "work_mode", "workspace_name", "workdir")
                             if key in source},
                            **home_fields(source, now=origin["at"]),
                            binding_revision=1, workspace_locked=True, harness_locked=True, forked_from=origin)
                write_json(target.history_path.parent / "topic.json", data, ensure_ascii=False, indent=2)
            new_label = topic_label(ctx.state_dir, target.topic_id)
            mode_tag = work_mode_tag(source.get("work_mode"))
            header = (f"**{TITLE}" + (f" · 话题 {new_label}" if new_label else "") + "** · 来自"
                      + ("上方转发的话题" if forwarded else "话题")
                      + (f" {source_label}" if source_label else "") + f"，任务 #{record['job_id']} 之后 · "
                      f"{time.strftime('%m-%d %H:%M')}\n"
                      f"原问题：{cut(record.get('user_text'), 80, beyond=True)}\n"
                      f"沿用原话题配置：{self.model_name(ctx, source.get('model') or '')} · "
                      f"思考预算 {cut(source.get('effort') or '模型默认', 20, beyond=True)}"
                      + (f" · {mode_tag}" if mode_tag else "")
                      + "；"
                      f"工作区 {cut(source.get('workspace_name'), 40, beyond=True)}\n"
                      "直接在本话题回复即可继续，原话题不受影响。")
            self.messenger.send_message(ctx.profile, chat, header, self.voice.key("fork-header", token), reply_to_message_id=root, voice=self.voice)
            with LOCK:
                source = read_json(source_path)
                source.setdefault("forks", []).append({"token": token, "mode": "fork", "at": origin["at"],
                                                       "topic_id": target.topic_id, "root_id": root,
                                                       "job_id": record["job_id"]})
                write_json(source_path, source, ensure_ascii=False, indent=2)
            self.runtime.log(f"opened fork topic={target.topic_id[:12]} from topic={ctx.topic_id[:12]} job=#{record['job_id']}")
            return source["forks"]

    def model_name(self, ctx, model):
        """The source's model as the model selects, receipt and task card name it (Claude · Opus 5.5);
        by its id when the catalog does not list it, not as the bare alias (Claude · opus)."""
        label = self.cards.model_label(ctx, model) if model else ""
        return label or f"{self.harness.spec.title} · {cut(model or '默认模型', 60, beyond=True)}"

    def forward_thread(self, ctx, chat, thread_id, token):
        """The source thread as one merged message in ``chat``: the thread itself, else a merge of its newest
        messages (THREAD_PAGE); the new message's ID, '' when none went."""
        sent = self.messenger.cardkit(ctx.profile, "POST", f"/open-apis/im/v1/threads/{thread_id}/forward",
                                      {"receive_id": chat}, params={"receive_id_type": "chat_id", "uuid": "fork-" + token})
        root = ((sent or {}).get("data") or {}).get("message_id") or ""
        if root:
            return root
        from covey.feishu.message_read import list_thread_messages
        items = [m for m in list_thread_messages(thread_id, ctx.profile, THREAD_PAGE)
                 if m.get("message_id") and not m.get("deleted") and m.get("msg_type") not in ("system", "merge_forward")]
        for attempt, kinds in enumerate((None, {"text", "post"})):
            ids = [m["message_id"] for m in items if kinds is None or m.get("msg_type") in kinds]
            if not ids:
                continue
            sent = self.messenger.cardkit(ctx.profile, "POST", "/open-apis/im/v1/messages/merge_forward",
                                          {"receive_id": chat, "message_id_list": ids},
                                          params={"receive_id_type": "chat_id", "uuid": f"fork-merge{attempt}-{token}"})
            data = (sent or {}).get("data") or {}
            root = (data.get("message") or {}).get("message_id") or data.get("message_id") or ""
            if root:
                self.runtime.log(f"fork thread not forwarded whole thread={thread_id}; merged its newest {len(ids)} messages")
                return root
        return ""

    def fork_root_card(self, ctx, chat, record, token, source_label):
        """A short root message for a topic whose thread cannot be forwarded; '' when it cannot be sent."""
        text = (f"**{TITLE}** · 来自话题" + (f" {source_label}" if source_label else "")
                + f"，任务 #{record['job_id']} 之后\n"
                "飞书没有接受原话题的转发，新话题从这条消息开始；原话题的会话背景会带给模型。\n"
                f"原问题：{cut(record.get('user_text'), 80, beyond=True)}")
        return self.messenger.create_and_send_card(ctx.profile, chat, notice_card(text, TITLE),
                                                   self.voice.key("fork-root", token),
                                                   voice=self.voice)[1]

    def fork_start(self, ctx, history_snapshot):
        """First run of a forked topic: fork the native session, or use the source's finished conversation.

        Only the first: a fork with a conversation of its own (``history_snapshot``) runs without a native session
        again only after 🔁 换配置, and goes on from its own record and the switch handoff, not from the source. The
        source's session is forked only on the Harness it belongs to: T750, forked from a topic on Antigravity and
        then switched to Claude Code, had the Antigravity session handed to ``claude --resume`` and failed every turn."""
        if history_snapshot:
            return ctx, history_snapshot
        try:
            origin = read_topic(ctx).get("forked_from")
        except (OSError, ValueError, KeyError):
            return ctx, history_snapshot
        if not origin:
            return ctx, history_snapshot
        busy = self.runtime.jobs.in_topic(origin["topic_id"], started=True)
        if not busy:
            # Its record too: the source may run in another task runner.
            busy = any(r.get("status") in {"running", "cancel_requested"}
                       for _, r in directory_store(ctx.state_dir / "jobs").query(topic_id=origin["topic_id"]))
        # Forks made before harness_id was recorded were bound to the source's Harness and have not switched yet.
        same = (origin.get("harness_id") or self.harness.spec.name) == self.harness.spec.name
        if origin.get("session_id") and same and not busy and backend_capabilities(self.harness.backend, ctx).native_fork:
            return dataclasses.replace(ctx, options={**ctx.options, "fork_session": origin["session_id"]}), history_snapshot
        return ctx, recent_history(Path(origin["history_path"]))
