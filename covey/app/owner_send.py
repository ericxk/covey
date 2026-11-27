"""代发: a message a topic drafted, sent as the owner by the 代答 sender once the owner confirmed it on its card
(docs/proxy.md 代发). A topic that has to tell someone something as the owner otherwise has no way: lark-cli as the
owner may lack im:message.send_as_user, the bot may not see the person, and the text ends in a file the owner forwards
by hand. The 代答 sender sends as the owner; it took only a 代答 batch.

A topic puts ``send_as_owner`` into its checkpoint (core/owner_send.py ``drafts``; the rule each turn reads,
``turn_note``). Once its runner exited and the turn ended well with an authorized reply (rule 5), the Bridge
(``collect``) finds the recipient with the owner's login (``resolve``: e-mail, open_id of the owner's app, a name,
or a chat) and sends the 代发草稿 card in the topic's thread (cards/owner_send.py), and a line at the chat's top level
pointing to it. Only the owner's click sends (app/access.py, through the caller): 发送 sends what the card's box holds,
the owner may have changed it; 不发 drops it; a name that found several people offers each to choose; 先问一句 (``ask``)
takes the owner's question about the draft to its topic and leaves the draft as it is. The click only
decides, under the record's lock (records, no Feishu: rule 1), and the send runs after the acknowledgement (``send``):
under Proxy.web_lock (one run of the send tool at a time, with 代答's sends and its QR login), through the sender of
the ``proxy_sender`` hook (ProxySend.web_send) with the draft standing in for a 代答 contact and batch, so its key is
the same for the same text (a send tried again is the tool's record answering). Certainly not sent: the card says why
and offers 重新发送; may have been sent: read back as the owner, else ``uncertain``, never sent again (rules 1 and 6).
Each outcome is drawn on the card and said in a line in the topic; the topic's later turns read it (``turn_note``), with
the drafts still waiting on their cards.

A topic does not pile up cards the owner has to clear (T825): a draft of an id the topic drafted before supersedes the
older one still waiting; once its card is out, a draft to the same person or chat supersedes the topic's older ones
waiting from other turns (not the ones this turn wrote again); an entry {"id", "withdraw": true} takes one back
(``withdraw``: declined, its card says the topic took it back); and a draft a later turn copied as it was (same id, same
text) while that one waits, is being or was sent, gets no second card.

Off without a sender (``enabled``: proxy_send.available) or with COVEY_OWNER_SEND=0: no rule in the turns, a draft in
a checkpoint gets one line in the topic saying it cannot be sent here. Not in a 代答 topic, nor the butler's private
chat. Records: <state>/owner-send/ (core/owner_send.py), written by the Bridge alone.
"""
from __future__ import annotations

import dataclasses
import functools
import re
import time
from pathlib import Path

from covey.app.answers import ask_id, ask_title
from covey.app.card_actions import form_fields
from covey.app.card_lanes import after_ack
from covey.app.form_cards import replace_form_card
from covey.app.proxy_commands import chat_info, search_people
from covey.app.proxy_send import available
from covey.cards.owner_send import (ASK, ASK_FIELD, ASK_LABEL, DROP, PICK, PICKS, SEND, TEXT_FIELD, editable,
                                    owner_send_card, top_card)
from covey.core import env as cenv
from covey.core import owner_send as records
from covey.core import turn_source
from covey.core.butler import is_butler_topic
from covey.core.checkpoint import read_checkpoint
from covey.core.jobs import context_for_job, find_job_record
from covey.core.locks import resource_lock
from covey.core.topics import proxy_topic, topic_label
from covey.feishu.cardkit import extract_card_id

ENDED_WELL = ("done", "waiting_user")
WEB_WAIT = 120  # Seconds a confirmed send waits for the send tool (a 代答 send or its QR login holding it).
NOT_SENT = ("validation", "unsupported", "auth", "failed", "launch")
OWNER_ONLY = "⚠️ 仅机器人主人可以发送或放弃代发草稿。"
CHANGED = "⚠️ 卡片刚更新过，草稿已经换了，这次点击没有执行。请看卡上现在的内容再点。"
EMPTY = "⚠️ 正文是空的，没有发送。"
TOO_LONG = f"⚠️ 正文超过 {records.TEXT_LIMIT} 字，没有发送。"
OFF = "⚠️ 这一轮写了以你的身份发消息的草稿，但这个部署不能以你的身份发（没有代答发送器，或 COVEY_OWNER_SEND=0），没有发。"
BLANK = "⚠️ 正文里还有没填的占位符（{shown}），没有发送。请在框里改掉再点发送。"
BLANK_FIXED = "⚠️ 正文里还有没填的占位符（{shown}），没有发送；这条在卡上不能改，请在话题里说，让它重写草稿。"
# 先问一句 (T604 #13): what the topic's model reads (``ask_message``), and the lines the topic gets when nothing was asked.
ASK_START = ("用户看了你起草的代发消息，先问一句。还没发出：草稿卡还开着，等他确认。这一轮只回答他问的；"
             "他要你改，就在检查点里用同一个 id 重写这条草稿。")
ASK_EMPTY = f"⚠️ 先问一句没有问：先在代发草稿卡上“{ASK_LABEL}”那一行写上你想问的，再点“{ASK_LABEL}”。"
ASK_GONE = "⚠️ 先问一句没有问：写这条草稿的话题找不到了（换了 Harness 或已关闭），可以直接在话题里说。"
WITHDRAWN = "话题撤回了这条草稿"
WITHDRAWN_NOTICE = "话题撤回了这条草稿，不发了。"
REPEATS = ("drafted", "unsent", "sending", "sent", "uncertain")  # An older draft of the same id and text: no second card.


def enabled() -> bool:
    """Whether 代发 is offered: a sender (the ``proxy_sender`` hook) and COVEY_OWNER_SEND not 0."""
    return available() and cenv.get("COVEY_OWNER_SEND", "1").strip() != "0"


def allowed(job, ctx, access=None) -> str:
    """'' when ``job``'s turn may offer 代发 drafts (WAVE12 C2), else why not, one word for the log; the one test of
    ``turn_note`` (the rule a turn is told) and ``collect`` (the drafts of its checkpoint), so no turn is told a rule
    it may not use. Not when:
    - ``source``: its 轮次来源 drafts none (core/turn_source.py ``owner_send``: a 协作 ask, a lead's wake-up, a lead's answer
      for the owner, 话题转交管家, a kind this release does not know);
    - ``not-his``: a stamped turn whose words are not the owner's level (a lead's work for a topic, the butler's hand-on of
      no message of his, a 协作 reply; core/turn_source.py ``level``): RT-2, the words an agent sent cannot draft in his name;
    - ``proxy-topic`` (a 代答 topic), ``main-thread`` (the butler's family, its lead topics too), ``read-only`` (a turn held
      to reading);
    - ``not-owner``: the message that started the turn is someone else's (app/access.py ``owner_turn`` of its sender: a
      group member's, in a topic of his own), when ``access`` is given; a turn that kept no sender (a hand-on, a
      continuation, an older record) is judged by its source above, as before.
    A turn of no source (the owner's message, an older release's hand-on or continuation) is judged by the rest, as before."""
    chat = str(getattr(job, "chat_id", "") or (job.get("chat_id") if isinstance(job, dict) else "") or "")
    topic = str(getattr(job, "topic_id", "") or (job.get("topic_id") if isinstance(job, dict) else "")
                or getattr(ctx, "topic_id", "") or "")
    if not turn_source.behavior(job).owner_send:
        return "source"
    if turn_source.of(job) is not None and turn_source.level(job) != turn_source.LEVEL_OWNER:
        return "not-his"
    if proxy_topic(ctx):
        return "proxy-topic"
    if topic and is_butler_topic(topic, chat):
        return "main-thread"
    if (getattr(job, "read_only", False) if not isinstance(job, dict) else job.get("read_only")) is True:
        return "read-only"
    sender = str(getattr(job, "sender_id", "") or (job.get("sender_id") if isinstance(job, dict) else "") or "")
    if access is not None and sender:
        try:
            if not access.owner_turn(chat, sender):
                return "not-owner"
        except Exception:  # noqa: BLE001 - not known to be his
            return "not-owner"
    return ""


def turn_note(ctx, job, access=None) -> str:
    """What a topic's turn is told after its message while 代发 is on: the rule, and what became of the topic's
    drafts that ended lately (core/owner_send.py turn_lines). '' off, or for a turn that may draft none (``allowed``:
    a 代答 topic, the butler's chat, an agent's words, a group member's turn). Read only: a task runner calls it."""
    try:
        topic = str(getattr(job, "topic_id", "") or getattr(ctx, "topic_id", "") or "")
        if not enabled() or not topic or allowed(job, ctx, access):
            return ""
        waiting = records.open_lines(ctx.state_dir, topic)
        lines = records.turn_lines(ctx.state_dir, topic)
        return ("\n\n" + records.RULE + ("\n" + records.OPEN_HEAD + "\n" + "\n".join(waiting) if waiting else "")
                + ("\n之前的代发草稿：\n" + "\n".join(lines) if lines else ""))
    except Exception:  # noqa: BLE001 - a turn never fails for its note
        return ""


def untaken(ctx, job, prior):
    """``prior``, the earlier checkpoint a turn is shown (app/runs.py _checkpoint_note), without the ``send_as_owner``
    entries the Bridge took in already (a record of that id in the topic): where they stand is in ``turn_note``, and a
    turn that copies them back as they were only makes a card it never meant (T825). Those never taken in (their turn
    failed) stay. ``prior`` itself when there is nothing to leave out; never raises. Read only: a task runner calls it."""
    try:
        topic = str(getattr(job, "topic_id", "") or getattr(ctx, "topic_id", "") or "")
        nested = isinstance(prior, dict) and isinstance(prior.get("checkpoint"), dict)
        checkpoint = prior.get("checkpoint") if nested else prior
        entries = checkpoint.get(records.FIELD) if isinstance(checkpoint, dict) else None
        if not topic or not isinstance(entries, list) or not entries:
            return prior
        taken = {record.get("ref") for record in records.topic_records(ctx.state_dir, topic)}
        left = [entry for entry in entries if not (isinstance(entry, dict) and str(entry.get("id") or "").strip()
                                                   and " ".join(str(entry["id"]).split())[:records.REF_CHARS] in taken)]
        if len(left) == len(entries):
            return prior
        checkpoint = {k: v for k, v in checkpoint.items() if k != records.FIELD} | ({records.FIELD: left} if left else {})
        return {**prior, "checkpoint": checkpoint} if nested else checkpoint
    except Exception:  # noqa: BLE001 - the note goes on as it was
        return prior


def ask_message(record, words) -> str:
    """What the topic's model reads for a 先问一句 on draft ``record``: what it is (not a decision on the draft), which
    draft (its id and whom it is for; not its text, which may hold a key), and the owner's ``words``."""
    who = record.get("to_label") or (record.get("to") or {}).get("name") or "对方"
    return f"{ASK_START}\n草稿 {record.get('ref') or ''}，发给 {who}。\n他问：\n{words}"


def person(candidate) -> dict:
    """A proxy_commands ``person`` as the recipient a record keeps."""
    return {"kind": "p2p", "open_id": candidate["target"], "chat_id": candidate.get("chat_id") or "",
            "name": candidate.get("name") or "", "department": "" if candidate.get("detail") == "人" else candidate.get("detail") or "",
            "email": candidate.get("email") or ""}


def resolve(profile, to) -> tuple[dict, list, str]:
    """(recipient, candidates, why neither) of a draft's ``to`` with the owner's login ``profile``: a chat by its id; a
    person by e-mail, by an open_id of the owner's app, else by name (several of that name: the candidates to choose)."""
    if not profile:
        return {}, [], "没有配置主人身份的 lark-cli profile（COVEY_PROXY_PROFILE）"
    if to.get("chat_id"):
        try:
            chat = chat_info(profile, to["chat_id"])
        except RuntimeError as exc:
            return {}, [], f"查不到会话 {to['chat_id']}：{str(exc)[:80]}"
        if not chat:
            return {}, [], f"查不到会话 {to['chat_id']}"
        return {"kind": "group" if chat.get("chat_mode") == "group" else "p2p", "chat_id": to["chat_id"],
                "name": chat.get("name") or ""}, [], ""
    tried = []
    for kind in ("email", "open_id", "name"):
        query = to.get(kind)
        if not query:
            continue
        try:
            found = [person(c) for c in search_people(profile, query)]
        except RuntimeError as exc:
            tried.append(f"按{kind}查失败：{str(exc)[:60]}")
            continue
        if kind == "email":
            found = [p for p in found if p["email"].lower() == query.lower()]
        elif kind == "name":
            found = [p for p in found if p["name"] == query] or found
        if len(found) == 1:
            return found[0], [], ""
        if len(found) > 1:
            return {}, found[:PICKS], ""
        tried.append(f"按{kind}没查到")
    return {}, [], "；".join(tried) or "没有收件人"


class OwnerSend:
    """Proxy's ``owner_send``: drafts in, cards out, clicks, sends."""

    def __init__(self, proxy) -> None:
        self.proxy = proxy

    @staticmethod
    def lock(ctx, draft_id):
        return resource_lock("owner-send", records.path(ctx.state_dir, draft_id))

    # Drafts in ----------------------------------------------------------------------------------------------

    def collect(self, job, ctx) -> None:
        """The drafts of ``job``'s finished turn, each opened (``open``), as the module says. Never raises."""
        try:
            record = find_job_record(ctx.state_dir, str(getattr(job, "message_id", "") or ""))
            path = str((record or {}).get("checkpoint_path") or "")
            checkpoint = read_checkpoint(Path(path)) if path else {}
            drafts, withdrawn = records.drafts(checkpoint), records.withdrawals(checkpoint)
            if not drafts and not withdrawn:
                return
            jctx = context_for_job(ctx, job)
            reason = ("not-finished" if record.get("status") not in ENDED_WELL
                      else "not-authorized" if not record.get("verified_reply_authorized")
                      else allowed(record, jctx, getattr(self.proxy.runtime, "access", None)))
            if reason:
                self.proxy.runtime.log(f"owner send ignored reason={reason} job=#{record.get('job_id')}")
                return
            if not enabled():
                self.proxy.messenger.send_message(jctx.profile, str(record.get("chat_id") or ""), OFF,
                                                  self.proxy.voice.key("owner-send-off", record.get("key") or ""),
                                                  voice=self.proxy.voice, reply_to_message_id=jctx.reply_to_message_id)
                return
            for ref in withdrawn:
                self.withdraw(jctx, record, ref)
            kept = {draft["ref"] for draft in drafts}
            for draft in drafts:
                self.open(jctx, record, draft, kept)
        except Exception as exc:  # noqa: BLE001 - the turn's ending goes on; the checkpoint keeps the drafts
            self.proxy.runtime.log(f"owner send collect failed job=#{getattr(job, 'job_id', '?')}: {type(exc).__name__}: {str(exc)[:200]}")

    def withdraw(self, ctx, job_record, ref) -> None:
        """The topic's drafts of id ``ref`` still waiting for the owner (records.OPEN), taken back: declined, the card
        redrawn saying the topic took it back. Not this turn's own (it cannot draft and take back one id at once)."""
        topic = str(job_record.get("topic_id") or "")
        for older in records.topic_records(ctx.state_dir, topic):
            if older.get("ref") != ref or older.get("status") not in records.OPEN or older.get("job_key") == job_record.get("key"):
                continue
            with self.lock(ctx, older["id"]):
                moved = records.move(ctx.state_dir, older, "declined", reason=WITHDRAWN)
                if moved and moved.get("message_id"):
                    self.publish(ctx, moved, {"state": "declined", "notice": WITHDRAWN_NOTICE,
                                              **({"hide": True} if (moved.get("view") or {}).get("hide") else {})})
            self.proxy.runtime.log(f"owner send withdrawn draft={older['id'][:8]} job=#{job_record.get('job_id')} "
                                   f"{'ok' if moved else 'stale'}")

    def supersede(self, ctx, record, older, reason) -> None:
        """``older``, still waiting for the owner, superseded by ``record``: its card redrawn."""
        with self.lock(ctx, older["id"]):
            moved = records.move(ctx.state_dir, older, "superseded", reason=reason, superseded_by=record["id"])
            if moved and moved.get("message_id"):
                self.publish(ctx, moved, {"state": "superseded",
                                          **({"hide": True} if (moved.get("view") or {}).get("hide") else {})})
        if moved:
            self.proxy.runtime.log(f"owner send superseded draft={older['id'][:8]} by={record['id'][:8]}")

    @staticmethod
    def repeated(state_dir, topic, job_key, draft) -> dict:
        """The topic's older draft (another turn's) that ``draft`` repeats as it was, same id and same text, while it waits,
        is being or was sent (REPEATS): a later turn that copied it gets no second card. {} for none."""
        text = records.digest(draft["text"])
        return next((older for older in records.topic_records(state_dir, topic)
                     if older.get("ref") == draft["ref"] and older.get("job_key") != job_key and not draft["problem"]
                     and older.get("status") in REPEATS and records.digest(older.get("text")) == text), {})

    def open(self, ctx, job_record, draft, kept=frozenset()) -> None:
        """One draft: its record (once: a turn read twice drafts once; none when it repeats a waiting or sent one,
        ``repeated``), the topic's older open draft of the same id superseded, the recipient looked up, the card sent in
        the topic's thread and a line at the top level; then the topic's older drafts to the same recipient from other
        turns superseded, but those of an id this turn wrote again (``kept``)."""
        state_dir = ctx.state_dir
        draft_id = records.record_id(str(job_record.get("key") or ""), draft["ref"])
        topic = str(job_record.get("topic_id") or "")
        with self.lock(ctx, draft_id):
            if records.read(state_dir, draft_id):
                return
        same = self.repeated(state_dir, topic, job_record.get("key"), draft)
        if same:
            self.proxy.runtime.log(f"owner send repeated draft={draft_id[:8]} as={same['id'][:8]} status={same.get('status')} "
                                   f"job=#{job_record.get('job_id')}: no second card")
            return
        with self.lock(ctx, draft_id):
            if records.read(state_dir, draft_id):
                return
            now = time.time()
            record = {"id": draft_id, "ref": draft["ref"], "status": "drafted", "job_key": job_record.get("key"),
                      "job_id": job_record.get("job_id"), "topic_id": topic, "chat_id": str(job_record.get("chat_id") or ""),
                      "reply_to": ctx.reply_to_message_id, "to": draft["to"], "text": draft["text"], "why": draft["why"],
                      "topic_label": topic_label(Path(state_dir), topic) if topic else "", "created_at": now,
                      "updated_at": now, "history": [["drafted", now, ""]]}
            records.write(state_dir, record)
        record = records.read(state_dir, draft_id)
        for older in records.topic_records(state_dir, topic):
            if older["id"] != draft_id and older.get("ref") == draft["ref"] and older.get("status") in records.OPEN:
                self.supersede(ctx, record, older, "话题出了同一 id 的新草稿")
        if draft["problem"]:
            recipient, candidates, reason = {}, [], draft["problem"]
        else:
            recipient, candidates, reason = resolve(self.proxy.settings(ctx).get("user_profile", ""), draft["to"])
        state = "drafted" if recipient else "choose" if candidates else "unresolved"
        with self.lock(ctx, draft_id):
            record = {**records.read(state_dir, draft_id), "recipient": recipient, "candidates": candidates,
                      "reason": reason, "to_label": recipient.get("name") or draft["to"].get("name") or ""}
            if state == "unresolved":
                record = records.move(state_dir, record, "declined", reason=reason, recipient={}, candidates=[],
                                      to_label=record["to_label"]) or record
            else:
                records.write(state_dir, record)
            view = {"state": state}
            card_id = extract_card_id(self.proxy.messenger.create_card(ctx.profile, owner_send_card(record, view)) or {})
            if not card_id:
                # Refused, maybe by Feishu's audit of what the text holds: once more without the text.
                view = {"state": state, "hide": True}
                card_id = extract_card_id(self.proxy.messenger.create_card(ctx.profile, owner_send_card(record, view)) or {})
            if not card_id:
                self.proxy.runtime.log(f"owner send card not created draft={draft_id[:8]}")
                return
            record = {**record, "card_id": card_id, "sequence": 0, "view": view}
            records.write(state_dir, record)
            message_id, _ = self.proxy.messenger.send_card(ctx.profile, record["chat_id"], card_id,
                                                           self.proxy.voice.key("owner-send", draft_id), voice=self.proxy.voice,
                                                           reply_to_message_id=record["reply_to"])
            if message_id:
                records.write(state_dir, {**record, "message_id": message_id})
        self.proxy.runtime.log(f"owner send drafted draft={draft_id[:8]} job=#{job_record.get('job_id')} state={state} "
                               f"card={'sent' if message_id else 'failed'}")
        if message_id and state == "drafted":
            mine = records.recipient_keys(record)
            for older in records.topic_records(state_dir, topic):
                if (older.get("status") in records.OPEN and older.get("job_key") != record.get("job_key")
                        and older.get("ref") not in kept and mine & records.recipient_keys(older)):
                    self.supersede(ctx, record, older, f"话题给{record.get('to_label') or '对方'}写了新的草稿")
        if message_id and state != "unresolved":
            self.top_line(ctx, record)

    def top_line(self, ctx, record) -> None:
        """A line at the chat's top level: a draft waits in the topic for the owner (M500: what the owner decides is not a
        sentence inside a thread). Never raises."""
        try:
            line = " · ".join(p for p in (record.get("topic_label"), f"有一条以你的身份发给{record.get('to_label') or '别人'}的草稿等你确认") if p)
            from covey.app.topic_links import topic_thread_link

            url = topic_thread_link(ctx.state_dir, record["topic_id"], profile=ctx.profile)[0]
            if url:
                line += f"（[到话题里看 ›]({url})）"
            top = dataclasses.replace(ctx, reply_to_message_id="")
            small = top_card(record.get("topic_label"), record.get("to_label"), url)  # A card; the line if it is refused.
            self.proxy.messenger.send_message(top.profile, record["chat_id"], line,
                                              self.proxy.voice.key("owner-send-top", record["id"]), voice=self.proxy.voice,
                                              card=small)
        except Exception as exc:  # noqa: BLE001 - the card is in the topic
            self.proxy.runtime.log(f"owner send top line failed draft={record['id'][:8]}: {type(exc).__name__}")

    # The card ----------------------------------------------------------------------------------------------

    def publish(self, ctx, record, view) -> bool:
        """The whole card of ``record`` drawn as ``view``, saved with it. The caller holds the record's lock."""
        path = records.path(ctx.state_dir, record["id"])
        return replace_form_card(ctx, path, record, {**record, "view": view}, owner_send_card(record, view),
                                 self.proxy.messenger, "owner-send")

    @staticmethod
    def matches(value):
        return value.get("kind") in (SEND, DROP, PICK, ASK)

    def claim(self, ctx, event, raw, owner):
        """The click's outcome when it is a 代发草稿 card's, else None (app/unified.py PANELS)."""
        return self.click(ctx, event, raw, owner) if self.matches(raw) else None

    def click(self, ctx, event, value, owner):
        draft_id = value.get("draft")
        if not isinstance(draft_id, str) or not re.fullmatch("[0-9a-f]{32}", draft_id):
            return "owner_send_unknown"
        with self.lock(ctx, draft_id):
            record = records.read(ctx.state_dir, draft_id)
            if not record.get("card_id") or record.get("message_id") != event.get("message_id"):
                return "owner_send_unknown"
            shown = record.get("view") or {}
            if not owner:
                self.publish(ctx, record, {**shown, "notice": OWNER_ONLY})
                return "owner_only"
            if value.get("kind") == ASK and record.get("status") in ("drafted", "unsent") and shown.get("state") != "choose":
                # The card is not redrawn: what the owner changed in the draft's box stays (T604 #13).
                words = (form_fields(event) or {}).get(ASK_FIELD)
                words = words.strip() if isinstance(words, str) else ""
                after_ack(functools.partial(self.ask, ctx, record, words, str(event.get("operator_id") or ""),
                                            str(event.get("event_id") or event.get("message_id") or "")))
                return "owner_send_ask" if words else "owner_send_ask_empty"
            view, outcome, record = self.decide(ctx, record, value, event, shown)
            self.publish(ctx, record, {**view, **({"hide": True} if shown.get("hide") else {})})
        if outcome == "owner_send":
            self.proxy.runtime.log(f"owner send confirmed draft={draft_id[:8]} edited={bool(record.get('edited'))}")
            after_ack(functools.partial(self.send, ctx, draft_id))
        return outcome

    def decide(self, ctx, record, value, event, shown):
        """(view, outcome, record to draw) of the owner's click. Under the record's lock: records only, no Feishu."""
        status, kind = record.get("status"), value.get("kind")
        state = "choose" if shown.get("state") == "choose" else status
        if status not in ("drafted", "unsent") or kind == ASK:  # A 先问一句 here: a card that offers it no more.
            return {"state": state if status in ("drafted", "unsent") else status}, "owner_send_stale", record
        if kind == PICK:
            pick = value.get("pick")
            candidates = record.get("candidates") or []
            if state != "choose" or not isinstance(pick, int) or isinstance(pick, bool) or not 0 <= pick < len(candidates):
                return {"state": state}, "owner_send_stale", record
            record = {**record, "recipient": candidates[pick], "candidates": [], "to_label": candidates[pick].get("name") or ""}
            records.write(ctx.state_dir, record)
            return {"state": "drafted"}, "owner_send_pick", record
        if kind == DROP:
            moved = records.move(ctx.state_dir, record, "declined", reason="主人点了不发",
                                 by=str(event.get("operator_id") or ""))
            return ({"state": "declined"}, "owner_send_drop", moved) if moved else ({"state": status}, "owner_send_stale", record)
        if state == "choose" or not record.get("recipient"):
            return {"state": state}, "owner_send_stale", record
        if value.get("text") != records.digest(record.get("text")):
            return {"state": state, "notice": CHANGED}, "owner_send_stale", record
        if editable(record.get("text")) and not shown.get("hide"):
            text = (form_fields(event) or {}).get(TEXT_FIELD)
            text = text.strip() if isinstance(text, str) else ""
        else:
            text = str(record.get("text") or "")  # Drawn read only: what the topic wrote.
        if not text:
            return {"state": state, "notice": EMPTY}, "owner_send_refused", record
        if len(text) > records.TEXT_LIMIT:
            return {"state": state, "notice": TOO_LONG}, "owner_send_refused", record
        blanks = records.placeholders(text)  # T604 #41: a draft with a blank left in it is not sent as it is.
        if blanks:
            words = BLANK if editable(record.get("text")) and not shown.get("hide") else BLANK_FIXED
            return {"state": state, "notice": words.format(shown="、".join(blanks[:3]))}, "owner_send_refused", record
        moved = records.move(ctx.state_dir, record, "sending", text=text,
                             edited=bool(record.get("edited")) or text != record.get("text"),
                             confirmed_by=str(event.get("operator_id") or ""), attempts=int(record.get("attempts") or 0) + 1,
                             send_started_at=time.time(), reason="")
        return ({"state": "sending"}, "owner_send", moved) if moved else ({"state": status}, "owner_send_stale", record)

    def ask(self, ctx, record, words, sender, event_id) -> None:
        """先问一句 on draft ``record``'s card (T604 #13), after the click's acknowledgement: ``words`` go to the topic that
        drafted it the way a message of the topic goes (app/questions.py queue_ask: into the task it runs, else as its
        next turn), with the drafting turn's checkpoint; the draft stays as it is, unsent. A line in the topic when
        nothing was asked (no words; the topic gone or on a Harness this Bridge no longer has)."""
        topic, chat = str(record.get("topic_id") or ""), str(record.get("chat_id") or "")
        found = self.proxy.topics.target(ctx.state_dir, topic, chat) if words else None
        if found is None:
            self.tell(ctx, record, ASK_EMPTY if not words else ASK_GONE, f"ask-{event_id}")
            return
        app, target, _ = found
        drafted = find_job_record(ctx.state_dir, str(record.get("job_key") or "")) or {}
        prior = read_checkpoint(Path(drafted["checkpoint_path"])) if drafted.get("checkpoint_path") else None
        job = app.questions.queue_ask(target, chat, words, ask_message(record, words), ask_id(record["id"], event_id),
                                      title=ask_title(words), prior=prior, sender=sender)
        self.proxy.runtime.log(f"owner send asked draft={record['id'][:8]} job=#{getattr(job, 'job_id', None)}")

    def tell(self, ctx, record, words, key) -> None:
        """``words`` as a line in the draft's topic thread. Never raises."""
        try:
            self.proxy.messenger.send_message(ctx.profile, record["chat_id"], words,
                                              self.proxy.voice.key("owner-send-tell", f"{record['id']}-{key}"),
                                              voice=self.proxy.voice, reply_to_message_id=record.get("reply_to") or "")
        except Exception as exc:  # noqa: BLE001 - nothing was asked; the card stays
            self.proxy.runtime.log(f"owner send line failed draft={record['id'][:8]}: {type(exc).__name__}")

    # The send ----------------------------------------------------------------------------------------------

    def send(self, ctx, draft_id) -> None:
        """The confirmed draft sent as the owner (outside every record lock), its outcome kept, drawn and said."""
        proxy = self.proxy
        record = records.read(ctx.state_dir, draft_id)
        if record.get("status") != "sending":
            return
        settings = proxy.settings(ctx)
        recipient = record.get("recipient") or {}
        text = record["text"]
        contact = {"key": f"owner-send:{draft_id}", "kind": "p2p", "target": recipient.get("open_id") or "",
                   "chat_id": recipient.get("chat_id") or "", "name": recipient.get("name") or ""}
        batch = {"id": draft_id, "messages": [{"chat_id": recipient.get("chat_id") or ""}],
                 "send_started_at": record.get("send_started_at") or time.time(), "reply": text}
        if not proxy.web_lock.acquire(timeout=WEB_WAIT):
            result = {"ok": False, "submitted": False, "type": "busy",
                      "error": "发送工具一直被占用（代答在发或在扫码登录）"}
        else:
            try:
                result = proxy.web.web_send(ctx, settings, contact, batch, text)
            except Exception as exc:  # noqa: BLE001 - it may have run: counted as maybe sent
                result = {"ok": False, "submitted": True, "type": "error", "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
            finally:
                proxy.web_lock.release()
        found = {}
        if not result.get("ok") and result.get("submitted") and contact["chat_id"] and settings.get("owner_user_id"):
            found = proxy.web.read_back(settings, contact, batch, tries=2)
        if result.get("ok") or found:
            status, fields = "sent", {"message_id": result.get("message_id") or found.get("id") or "",
                                      "sent_at_text": time.strftime("%H:%M")}
        elif not result.get("submitted") or result.get("type") in NOT_SENT:
            status, fields = "unsent", {"reason": result.get("error") or "发送失败"}
            if result.get("type") == "auth":
                proxy.web_login.login_gone(ctx)
        else:
            status, fields = "uncertain", {"reason": result.get("error") or "没有结果"}
        with self.lock(ctx, draft_id):
            moved = records.move(ctx.state_dir, record, status, **fields)
            if moved:
                self.publish(ctx, moved, {"state": status, **({"hide": True} if (record.get("view") or {}).get("hide") else {})})
        proxy.runtime.log(f"owner send draft={draft_id[:8]} status={status} type={result.get('type')} "
                          f"message_id={fields.get('message_id') or None}")
        if moved:
            self.say(ctx, moved)

    def say(self, ctx, record) -> None:
        """The outcome as a line in the topic's thread. Never raises."""
        who = record.get("to_label") or "对方"
        words = {
            "sent": f"✅ 已以你的身份发给{who}（{record.get('sent_at_text', '')}）。",
            "unsent": f"⚠️ 代发给{who}的消息没发出：{record.get('reason') or '发送失败'}。处理好后在卡上点“重新发送”（要改正文就先在框里改）。",
            "uncertain": f"⚠️ 代发给{who}的消息可能已发出但没能确认，不会自动重发，请到会话里看一眼。",
        }[record["status"]]
        try:
            self.proxy.messenger.send_message(ctx.profile, record["chat_id"], words,
                                              self.proxy.voice.key("owner-send-said", f"{record['id']}-{len(record['history'])}"),
                                              voice=self.proxy.voice, reply_to_message_id=record.get("reply_to") or "")
        except Exception as exc:  # noqa: BLE001 - the card shows it
            self.proxy.runtime.log(f"owner send line failed draft={record['id'][:8]}: {type(exc).__name__}")


def collect(gateway, job, ctx) -> None:
    """After a task's runner exited (app/detached.py, app/job_dispatch.py): its drafts. Nothing without the Bridge's proxy,
    nor for a turn that may draft none (``allowed``, which ``OwnerSend.collect`` asks of its record)."""
    if not turn_source.behavior(job).owner_send:
        return
    proxy = getattr(gateway, "proxy", None)
    if proxy is not None and hasattr(proxy, "owner_send"):
        proxy.owner_send.collect(job, ctx)
