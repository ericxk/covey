"""Proxy replies (代答), the Bridge's side: polling the whitelisted chats as the owner, one topic per
chat in the proxy group, and each batch's way from the messages taken in to the answer sent as the owner.

- Its parts: taking messages in, the worker ``listen`` (app/proxy_intake.py, ``intake``); the contacts'
  topics (app/proxy_topics.py, ``contact_topics``); the owner's commands, settings and lookups
  (app/proxy_commands.py, ``commands``); sending as the owner (the sender tool) and reading the owner's
  chat (app/proxy_send.py, ``web``); the send tool's login (app/proxy_login.py, ``web_login``); the 代答设置
  card (app/proxy_panel.py, ``panel``); the 重新发送 card (app/proxy_resend.py, ``resend``).
- What ``listen`` finds waits in its inbox; ``run``, the other worker and the only one that writes batches,
  takes it in at once (``wake``) and every POLL_SECONDS, or when a job of a 代答 topic ends. New messages of a
  chat become a batch; a batch becomes a
  job in that chat's topic, bound like a fork so it starts without a launch card, once the chat has been quiet for
  SETTLE_SECONDS (SETTLE_LIMIT at most). Newer messages stop a round under way and go with its own into the next
  one (``add_messages``: OVERTAKEN, MAX_RESTARTS in a row); a send under way is never stopped. Its saved and
  authorized reply is sent as the owner by the sender tool (app/proxy_send.py), a reply inside a group's
  thread inside that thread. A reply starting with SKIP_MARK, a run without a
  reply or a send that certainly did not go out holds the batch: the owner's next turn in the topic answers
  it, and a send that did not go out on any try gets a 重新发送 card (app/proxy_resend.py) that
  sends the saved reply again. A send that may have gone out is never sent again (``uncertain``).
  Nothing goes out while 代答 is off or the contact paused (held), for a removed contact (dropped), or
  as a batch's own reply past STALE_AFTER unless the owner clicked 重新发送 (held): ``stopped``, read
  fresh, and again under ``lock`` just before ``sending``.
Records and their rules: core/proxy.py.
"""
from __future__ import annotations

import subprocess
import threading
import time

from covey.core import env as cenv
from covey.app import runner_ipc
from covey.app.proxy_commands import ProxyCommands
from covey.app.proxy_intake import ProxyIntake
from covey.app.proxy_panel import ProxyPanel
from covey.app.owner_send import OwnerSend
from covey.app.proxy_resend import ProxyResend
from covey.app.proxy_send import (READ_BACKS, NullLogin, ProxySend, available, clock, message_epoch, minute,
                                           plain_mentions)
from covey.core import extensions
from covey.app.proxy_topics import ProxyTopics
from covey.cards.elements import notice_card
from covey.core import proxy as records
from covey.core.jobs import JOB_STOP_ACTION_KIND, find_job_record, job_key, topic_job_records
from covey.core.statuses import NOT_ENDED
from covey.core.saved_results import load_authorized_reply
from covey.core.history_storage import append_history

POLL_SECONDS = 30  # ``run``'s round when nothing wakes it sooner.
# A batch is submitted once its chat has been quiet this long (a burst of messages is one round, not a round
# stopped for each)...
SETTLE_SECONDS = 10
SETTLE_LIMIT = 45  # ...and at the latest this long after it was opened, however the messages keep coming.
LATER = "later"  # ``decide``/``send``: held off by HOURLY_LIMIT or the tool's login; the same turn is tried again later.


def unsettled(batch, now):
    """Seconds ``batch`` still waits for its chat to settle (SETTLE_SECONDS, SETTLE_LIMIT); 0 once it may run."""
    opened = float(batch.get("created_at") or 0)
    last = float(batch.get("last_in_at") or opened)
    return max(0.0, min(last + SETTLE_SECONDS, opened + SETTLE_LIMIT) - now)


def removed(batch):
    """A stand-in for the contact of ``batch`` after it left the whitelist: enough to read a send back."""
    kind, _, target = batch["contact"].partition(":")
    return {"key": batch["contact"], "kind": kind, "target": target, "name": "",
            "chat_id": batch["messages"][-1]["chat_id"]}


class Proxy:
    """Holds no state of its own beyond ``lock`` (commands on the consumer thread and ``run`` on its
    worker read-modify-write the same records), ``web_lock`` (one send or login of the send tool at a time), and
    in memory: ``wake`` (``run`` goes round now; ``intake`` sets it for what it found) and ``soon``: seconds to
    the next batch that settles. Its parts hold the rest (``intake``: the inbox and the reads' state)."""

    def __init__(self, topics) -> None:
        self.topics = topics
        self.runtime, self.messenger, self.voice = topics.runtime, topics.messenger, topics.voice
        self.lock = records.LOCK  # move_batch's own.
        self.web_lock = threading.Lock()
        self.wake = threading.Event()
        self.soon = None  # ``tick``: a received batch settles in that many seconds; ``run`` goes round then.
        self.runtime.job_ended.append(self.job_ended)
        self.intake = ProxyIntake(self)
        self.contact_topics = ProxyTopics(self)
        self.commands = ProxyCommands(self)
        hook = extensions.hook("proxy_sender")  # None: no sender, so no 代答 (``available``).
        self.web = ProxySend(self, hook.Sender(self) if hook else None)
        self.web_login = hook.ProxyLogin(self) if hook else NullLogin(self)
        self.panel = ProxyPanel(self)
        self.resend = ProxyResend(self)
        self.owner_send = OwnerSend(self)  # 代发 (app/owner_send.py): a topic's draft, sent as the owner once confirmed.

    def settings(self, ctx):
        settings = records.read_settings(ctx.state_dir)
        settings.setdefault("user_profile", cenv.get("COVEY_PROXY_PROFILE", ""))
        settings.setdefault("send_tool", hook.tool_dir() if (hook := extensions.hook("proxy_sender")) else "")
        settings.setdefault("send_home", str(records.proxy_dir(ctx.state_dir) / "send-as-user"))
        return settings

    def threads(self):
        """Its two workers, always when there is a sender: each reads whether 代答 is on every round
        (app/unified.py worker_targets). None without one."""
        if not available():
            return ()
        return (("proxy", self.run), ("proxy-in", self.intake.listen))

    def menu_items(self):
        """The bot menu's 代答 (app/unified.py menu); none without a sender."""
        return {} if not available() else {"proxy_settings": self.panel.open}

    def menu_folds(self):
        """A second 代答 on its card, the newest in the chat (app/bot_menu.py)."""
        return {} if not available() else {"proxy_settings": self.panel.fold}

    # The worker ------------------------------------------------------------------------------

    def run(self, ctx):
        while not self.rest(POLL_SECONDS if self.soon is None else min(POLL_SECONDS, self.soon + 0.1)):
            settings = self.settings(ctx)
            # Off, nothing is taken in, and the batches still open move on without being sent (``stopped``).
            if not settings.get("owner_user_id") or not (settings.get("enabled") or records.read_batches(ctx.state_dir)):
                self.intake.drain_inbox()  # Found just before 代答 went off: not taken in once it is on again.
                continue
            try:
                self.tick(ctx, settings)
            except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as exc:
                self.runtime.log(f"proxy tick failed: {type(exc).__name__}: {str(exc)[:200]}")

    def rest(self, seconds):
        """Wait ``seconds``, or until ``wake``; True once the Bridge is stopping."""
        deadline = time.monotonic() + seconds
        while not self.runtime.shutdown.is_set():
            left = deadline - time.monotonic()
            if left <= 0 or self.wake.wait(min(left, 1)):
                self.wake.clear()  # Set again while ``tick`` runs, the next round starts right after it.
                return self.runtime.shutdown.is_set()
        return True

    def job_ended(self, job):
        """Runtime.job_ended: a job of the proxy group (a 代答 topic's) ended, so its batch moves on now
        (``check_job``, ``check_held``), not on the next round."""
        if self.runtime.state_dir is not None and job.chat_id \
                and job.chat_id == records.read_settings(self.runtime.state_dir).get("group_chat_id"):
            self.wake.set()

    def tick(self, ctx, settings):
        # ``listen`` reads; only this worker writes batches, so a take-in never races a batch's move on. The
        # batches move on whatever the reads do (repo review D13: a search killed at rc=-15 once skipped a poll).
        self.soon = None
        inbox = self.intake.drain_inbox()
        if settings.get("enabled"):
            for key, messages in inbox.items():
                self.add_messages(ctx, key, messages)
        try:
            self.contact_topics.sync_topics(ctx, settings)
        except (RuntimeError, OSError, ValueError) as exc:
            self.runtime.log(f"proxy model sync failed: {type(exc).__name__}: {str(exc)[:200]}")
        self.web_login.check_login(ctx, settings)
        contacts = {c["key"]: c for c in records.read_contacts(ctx.state_dir)}
        self.web.read_backs(ctx, settings, contacts)
        for batch in records.read_batches(ctx.state_dir):
            if batch.get("stop_job"):  # Its stop not yet asked for: the Bridge stopped after add_messages wrote it.
                self.stop_overtaken(ctx, settings, batch)
        batches = records.read_batches(ctx.state_dir)
        now = time.time()
        for batch in batches:  # A batch under way (a held one: since it was held) makes its chat listed sooner.
            self.intake.mark_active(batch["contact"], float(batch.get("updated_at") or 0) if batch["status"] == "held" else now)
        for batch in batches:
            if self.runtime.shutdown.is_set():
                return
            try:
                self.advance(ctx, settings, contacts, batches, batch)
            except Exception as exc:  # noqa: BLE001 - one batch that cannot move on holds up no other
                # 2026-10-27: its topic could not open; a KeyError in one batch once stopped every contact.
                self.runtime.log(f"proxy batch={batch['id'][:8]} failed: {type(exc).__name__}: {str(exc)[:200]}")

    def advance(self, ctx, settings, contacts, batches, batch):
        """``batch`` one step along, as far as its contact, its job and the owner allow."""
        contact = contacts.get(batch["contact"])
        if contact is not None and float(contact.get("added_at") or 0) > float(batch.get("created_at") or 0):
            contact = None  # Removed and added again since: the batch was its earlier entry's.
        if contact is None and batch["status"] == "sending":
            contact = removed(batch)  # Its send ran when the Bridge stopped: the read-back still decides.
        elif contact is None:
            # A reply for a contact no longer on the whitelist is never sent, also once it is added again.
            self.resend.close(ctx, records.move_batch(ctx.state_dir, batch, "skipped", reason=records.REMOVED))
            return
        if batch["status"] == "received":
            if not settings.get("enabled") or contact.get("paused"):
                records.move_batch(ctx.state_dir, batch, "skipped",
                                   reason="已暂停代答" if settings.get("enabled") else "代答已关闭")
            elif not any(b["contact"] == batch["contact"] and b["status"] in {"submitted", "sending"} for b in batches):
                wait = unsettled(batch, time.time())
                if wait:
                    self.soon = wait if self.soon is None else min(self.soon, wait)
                else:
                    self.submit(ctx, settings, contact, batch)
        elif batch["status"] == "submitted":
            self.check_job(ctx, settings, contact, batch)
        elif batch["status"] == "held":
            self.check_held(ctx, settings, contact, batch)
        elif batch["status"] == "sending":
            # The Bridge stopped during a send: only a read-back decides; it is never sent again.
            self.finish_send(ctx, settings, contact, batch, {"submitted": True, "error": "发送中断"})

    def add_messages(self, ctx, key, messages):
        """New ``messages`` of a contact: they join its received batch (or open one) and replace a held one. A
        submitted one, its job running or ended but not yet decided, is OVERTAKEN: superseded, its messages carried
        into the received batch and its job asked to stop (``stop_overtaken``), so the next round answers all of
        them and its reply is never sent. Not after MAX_RESTARTS rounds stopped in a row, nor when the owner has a
        later turn in the topic (theirs is the reply that goes out, repo review D2): that round runs to its end as
        before. A sending one is never touched. Only records under ``lock``; the stop and the line in the topic come
        after it (rule 1)."""
        with self.lock:
            contact = next((c for c in records.read_contacts(ctx.state_dir) if c["key"] == key), None)
            # Found twice (listed and searched, or a round read before the last one was taken in): taken once.
            messages = [m for m in messages if contact is not None and m["id"] not in contact.get("seen", [])]
            if not messages:
                return
            contact["seen"] = [*contact.get("seen", []), *(m["id"] for m in messages)][-records.SEEN_LIMIT:]
            contact["chat_id"] = contact.get("chat_id") or messages[-1]["chat_id"]
            records.write_contact(ctx.state_dir, contact)
            open_batches = [b for b in records.read_batches(ctx.state_dir) if b["contact"] == key]
            replaced = [records.move_batch(ctx.state_dir, held, "superseded", reason="对方又发来新消息")
                        for held in open_batches if held["status"] == "held"]
            running = next((b for b in open_batches if b["status"] == "submitted"), None)
            later = running is not None and any(int(r["job_id"]) > int(running["job_id"])
                                                for r in topic_job_records(ctx.state_dir, running["topic_id"]))
            capped = running is not None and int(running.get("restarts") or 0) >= records.MAX_RESTARTS
            stopped = running if running is not None and not (later or capped) else None
            # A round running a short while is left to end, its reply unused: the next round, queued after it,
            # resumes its session (T571). One queued, or running long, is stopped as before.
            runs_on = stopped is not None and self.runs_on(ctx, stopped)
            carried = stopped["messages"] if stopped else []
            received = next((b for b in open_batches if b["status"] == "received"), None)
            if received:
                received.update(messages=records.merged(carried, received["messages"], messages), last_in_at=time.time())
            else:
                received = records.new_batch(contact, messages, carried)
            if stopped:
                received.update(restarts=max(int(received.get("restarts") or 0), int(stopped.get("restarts") or 0) + 1),
                                **({} if runs_on else {"stop_job": stopped["id"]}))
            # The new batch first: the Bridge stopping between the two writes still finds the stop to ask for.
            records.write_batch(ctx.state_dir, received)
            if stopped:
                stopped = records.move_batch(ctx.state_dir, stopped, "superseded",
                                             reason=records.OVERTAKEN_RUNS_ON if runs_on else records.OVERTAKEN)
        for batch in replaced:
            self.resend.close(ctx, batch)
        self.runtime.log(f"proxy took in {len(messages)} message(s) contact={records.digest(key)[:8]}")
        if stopped:
            self.runtime.log(f"proxy overtaken batch={stopped['id'][:8]} job=#{stopped['job_id']}: its "
                             f"{len(carried)} message(s) go into batch={received['id'][:8]} restarts={received['restarts']}"
                             + (", its job runs on for its session" if runs_on else ""))
            if runs_on:
                self.note(ctx, self.settings(ctx), stopped, "· 对方又发来新消息：这一轮跑完不发，下一轮接着它和新消息一起回",
                          tag="overtaken")
            else:
                self.stop_overtaken(ctx, self.settings(ctx), received)
        elif running is not None:
            self.runtime.log(f"proxy batch={running['id'][:8]} job=#{running['job_id']} runs on: "
                             + ("the owner's later turn in its topic" if later else
                                f"{records.MAX_RESTARTS} rounds stopped in a row"))

    def runs_on(self, ctx, batch):
        """Whether ``batch``'s job, overtaken by newer messages, is left to end rather than stopped: it is running
        (a queued one has nothing to keep) and began less than OVERTAKE_STOP_AFTER ago. Its native session is kept
        only by a turn that ends (app/turn_settle.py), and the next round, queued after it in the topic, resumes it."""
        record = find_job_record(ctx.state_dir, batch.get("job_key") or "") if batch.get("job_key") else None
        return ((record or {}).get("status") == "running" and records.OVERTAKE_STOP_AFTER > 0
                and time.time() - records.submitted_at(batch) < records.OVERTAKE_STOP_AFTER)

    def stop_overtaken(self, ctx, settings, batch):
        """The round whose messages ``batch`` took over (``stop_job``) stopped: superseded (already, unless the
        Bridge stopped between add_messages's writes), its job asked to stop (``stop_job``) and a line in the topic;
        then ``stop_job`` is cleared, also when the stop could not be asked for: the round is superseded, so its
        reply is never used whatever its job does."""
        with self.lock:
            gone = records.read_batch(ctx.state_dir, batch["stop_job"])
            if gone is not None and gone["status"] == "submitted":
                gone = records.move_batch(ctx.state_dir, gone, "superseded", reason=records.OVERTAKEN)
        try:
            if gone is not None and gone["status"] == "superseded" and gone.get("job_key"):
                self.stop_job(ctx, settings, gone)
                self.note(ctx, settings, gone, "· 对方又发来新消息，这一轮停下，和新消息一起回", tag="overtaken")
        except Exception as exc:  # noqa: BLE001 - its reply is unused all the same; the new round goes on
            self.runtime.log(f"proxy stop failed batch={batch['stop_job'][:8]}: {type(exc).__name__}: {str(exc)[:200]}")
        with self.lock:
            current = records.read_batch(ctx.state_dir, batch["id"])
            if current is not None and current.get("stop_job") == batch["stop_job"]:
                current.pop("stop_job")
                records.write_batch(ctx.state_dir, current)

    def stop_job(self, ctx, settings, batch):
        """``batch``'s job asked to stop the way its card's 停止 does: a job in a live task runner by that click
        forwarded to the runner, which stops its own job (rule 8); one in this Bridge (queued, or not handed off)
        by TaskControls.cancel_reply_job_from_card here. Nothing for a job that ended or is stopping already."""
        record = find_job_record(ctx.state_dir, batch["job_key"])
        status = (record or {}).get("status")
        contact = next((c for c in records.read_contacts(ctx.state_dir) if c["key"] == batch["contact"]), None)
        if status not in NOT_ENDED or status == "cancel_requested" or not (contact or {}).get("root_id"):
            self.runtime.log(f"proxy stop job=#{batch['job_id']} batch={batch['id'][:8]}: nothing to stop ({status})")
            return
        app, routed, target = self.contact_topics.topic_context(ctx, settings, contact, batch["mirror_id"])
        group, job_id = settings["group_chat_id"], int(record["job_id"])
        action = {"kind": JOB_STOP_ACTION_KIND, "job_id": str(job_id),
                  "message_id": str(record.get("message_id") or batch["mirror_id"]), "by": "proxy"}
        # The click on its task card, as the owner's: the runner routes it to the job like any click there.
        event = {**routed, "message_id": str(record.get("card_message_id") or batch["mirror_id"]),
                 "event_id": f"proxy-stop-{batch['id']}", "operator_id": self.runtime.owner_open_id or "",
                 "action_value": action}
        if runner_ipc.forward_card_action(self.runtime, target, event, action):
            how = "sent to its task runner"
        elif self.runtime.jobs.find(job_id, action["message_id"]) is not None:
            app.controls.cancel_reply_job_from_card(target, action, group, event["event_id"])
            how = "asked in the Bridge"
        else:
            how = "nothing to stop (no runner or job holds it)"
        self.runtime.log(f"proxy stop job=#{job_id} batch={batch['id'][:8]}: {how}")

    def submit(self, ctx, settings, contact, batch):
        """The batch as a job in the contact's topic: its messages shown there, then the job. A batch the
        owner has answered in the chat already runs nothing and shows nothing: they are talking themselves."""
        if not batch.get("mirror_id") and self.web.owner_replied(ctx, settings, contact, batch):
            records.move_batch(ctx.state_dir, batch, "skipped", reason="你已亲自回复")
            self.runtime.log(f"proxy skipped batch={batch['id'][:8]}: the owner replied")
            return
        contact = self.contact_topics.ensure_topic(ctx, settings, contact)
        if contact is None:
            return  # Removed meanwhile: the next poll drops the batch.
        shown = records.mirror_text(contact, batch)
        mirror = batch.get("mirror_id")
        if not mirror:
            _, mirror = self.messenger.create_and_send_card(
                ctx.profile, settings["group_chat_id"], notice_card(shown, "代答"),
                self.voice.key("proxy-in", batch["id"]), voice=self.voice, reply_to_message_id=contact["root_id"])
            if not mirror:
                return  # Tried again next poll; the key makes Feishu return the same message.
            batch = {**batch, "mirror_id": mirror}
            records.write_batch(ctx.state_dir, batch)
        app, event, target = self.contact_topics.topic_context(ctx, settings, contact, mirror)
        existing = [r for r in topic_job_records(ctx.state_dir, target.topic_id) if r.get("message_id") == mirror]
        if existing:
            job_id = int(existing[0]["job_id"])
        else:
            # The chat as it is (T571): the owner's own messages too, which the topic's history never has.
            prompt = records.batch_prompt(contact, batch, settings, records.read_style(ctx.state_dir),
                                          context=records.read_context(ctx.state_dir),
                                          recent=self.web.recent_chat(settings, contact))
            append_history(target.history_path, "user", prompt, {**event, "display_content": shown})
            last = batch["messages"][-1]
            job = app.submission.enqueue_chat_job(target, f"{last['sender_name']}：{last['content'][:200]}",
                                                  settings["group_chat_id"], mirror, prompt_content=prompt,
                                                  sender_id=self.runtime.owner_open_id or "")
            if job is None:
                # Not submitted (its model could not be confirmed, said in the topic): not again every poll.
                records.move_batch(ctx.state_dir, batch, "skipped", reason="任务未提交")
                return
            job_id = job.job_id
        records.move_batch(ctx.state_dir, batch, "submitted", job_id=job_id, job_key=job_key(mirror, job_id),
                           topic_id=target.topic_id, considered_job_id=job_id)
        self.runtime.log(f"proxy submitted job=#{job_id} batch={batch['id'][:8]}")

    def reply_of(self, ctx, settings, contact, record):
        """The saved, authorized reply of a finished job of the topic; None when there is none to use."""
        if record.get("status") != "done":
            return None
        app, _, target = self.contact_topics.topic_context(ctx, settings, contact, str(record.get("message_id") or contact["root_id"]))
        try:
            reply, _ = load_authorized_reply(target, record, app.harness.spec.name, settings["group_chat_id"])
        except (OSError, ValueError, KeyError) as exc:
            self.runtime.log(f"proxy reply unusable job=#{record.get('job_id')}: {exc}")
            return None
        return reply.text

    def check_job(self, ctx, settings, contact, batch):
        record = find_job_record(ctx.state_dir, batch["job_key"])
        if record is None or record.get("status") in NOT_ENDED:
            return
        later = [r for r in topic_job_records(ctx.state_dir, batch["topic_id"]) if int(r["job_id"]) > int(record["job_id"])]
        self.decide(ctx, settings, contact, batch, record, newer=min(later, key=lambda r: int(r["job_id"]), default=None))

    def check_held(self, ctx, settings, contact, batch):
        """A held batch: a send that certainly did not go out is tried again (SEND_ATTEMPTS, and again
        after the owner clicks 重新发送); else the owner's finished turns in the topic, in order, answer it:
        the newest one's reply goes out (``decide``). A newer batch of the contact replaces it: a batch
        taken in while this one still ran would otherwise wait for the same turn and send its reply twice."""
        if any(b["contact"] == batch["contact"] and b["created_at"] > batch["created_at"]
               for b in records.read_batches(ctx.state_dir)):
            self.resend.close(ctx, records.move_batch(ctx.state_dir, batch, "superseded", reason="对方又发来新消息"))
            return
        if batch.get("reply") and (batch.get("resend_requested") or batch.get("send_failed")
                                   and batch.get("attempts", 0) < records.SEND_ATTEMPTS):
            self.send(ctx, settings, contact, batch, batch["reply"], batch.get("reply_job_id"))
            return
        if batch.get("send_failed") and batch.get("reply") and not batch.get("resend_card"):
            # Held without its 重新发送 card: an older release said it in a line of text, or the card did not
            # go out the last time (resend.show). Both stay: this is also how that card is tried again.
            batch = self.resend.show(ctx, settings, batch, "failed") or batch
        seen = int(batch.get("considered_job_id") or 0)
        turns = sorted((r for r in topic_job_records(ctx.state_dir, contact["topic_id"]) if int(r["job_id"]) > seen),
                       key=lambda r: int(r["job_id"]))
        for at, record in enumerate(turns):
            if record.get("status") in NOT_ENDED:
                return
            # A turn after sends that never went out brings a new reply: it gets SEND_ATTEMPTS of its own.
            batch = self.update(ctx, batch, considered_job_id=int(record["job_id"]),
                                **({"attempts": 0, "send_failed": False, "relogin_at": None} if batch.get("send_failed") else {}))
            if batch is None:
                return
            outcome = self.decide(ctx, settings, contact, batch, record, newer=turns[at + 1] if at + 1 < len(turns) else None)
            if outcome == LATER:
                # Not past this turn: it is sent once the hour allows (repo review D14: it was dropped unsaid).
                self.update(ctx, batch, considered_job_id=seen)
                return
            if outcome:
                return
            seen = int(record["job_id"])

    def send_again(self, ctx):
        """The send tool logged in again (ProxyLogin.login_back): each held batch whose sends certainly did not go
        out on every try gets its tries again (``attempts`` 0, ``relogin_at``), and ``run`` goes round now:
        ``check_held`` sends it through every gate of ``send``. Their ids' heads."""
        again = []
        with self.lock:
            for batch in records.read_batches(ctx.state_dir, statuses={"held"}):
                if batch.get("send_failed") and batch.get("reply") and not batch.get("resend_requested") \
                        and int(batch.get("attempts") or 0) >= records.SEND_ATTEMPTS:
                    records.write_batch(ctx.state_dir, {**batch, "attempts": 0, "relogin_at": round(time.time())})
                    again.append(batch["id"][:8])
        if again:
            self.wake.set()
        return again

    def update(self, ctx, batch, **fields):
        """``batch`` with ``fields``, written over the batch as it is now (a click on its card may have
        written it since this poll read it); None, and nothing written, when it has moved on meanwhile."""
        with self.lock:
            current = records.read_batch(ctx.state_dir, batch["id"])
            if current is None or current["status"] != batch["status"]:
                return None
            current.update(fields)
            records.write_batch(ctx.state_dir, current)
        return current

    def decide(self, ctx, settings, contact, batch, record, *, newer=None):
        """Send ``record``'s reply for ``batch``, close it when nothing needs saying back, or hold it; True
        when it was sent, tried or closed, LATER when HOURLY_LIMIT holds the send off. ``newer`` is a later
        turn in the topic (queued, running or done): the owner spoke there after ``record`` began, and that
        turn was told its reply goes out (``owner_turn_note``), so a reply of ``record`` that would go out or
        close the batch is passed over and the batch held for it (repo review D2: the batch's own reply went
        out and the owner's never did)."""
        reply = self.reply_of(ctx, settings, contact, record)
        done = records.no_reply_needed(reply) if reply is not None else None
        reason = records.skip_reason(reply) if reply is not None else f"任务没有可用的回复（{record.get('status')}）"
        job_id = int(record["job_id"])
        if newer is not None and (done is not None or reason is None):
            if batch["status"] == "held":
                self.update(ctx, batch, passed_over=job_id)
            else:
                records.move_batch(ctx.state_dir, batch, "held", reason=records.LATER_TURN, considered_job_id=job_id,
                                   passed_over=job_id)
            self.runtime.log(f"proxy passed over batch={batch['id'][:8]} job=#{job_id}: "
                             f"the later turn #{newer['job_id']} in its topic answers it")
            return False
        if (done is not None or reason is not None) and batch["status"] == "submitted" and self.newer_batch(ctx, batch):
            # Newer messages came while it ran: the batch taking them in says what comes of them (repo review
            # U5: every superseded round said its own ⏸ line, the same reason and suggestion each time).
            records.move_batch(ctx.state_dir, batch, "superseded", reason="对方又发来新消息", considered_job_id=job_id)
            self.runtime.log(f"proxy batch={batch['id'][:8]} job=#{job_id} superseded by newer messages")
            return True
        if done is not None:
            why = done.splitlines()[0].strip() if done else "对方只是收尾或确认"
            skipped = records.move_batch(ctx.state_dir, batch, "skipped", reason="不用回：" + why, considered_job_id=job_id)
            if skipped is None:
                return True  # Moved on meanwhile.
            # Why is in the turn's answer just above: said once (U5).
            self.note(ctx, settings, skipped, f"· 不用回（原因见上面 #{job_id} 的回答）" if done else "· 不用回：" + why)
            self.resend.close(ctx, skipped)
            return True
        if reason is None:
            return self.send(ctx, settings, contact, batch, reply, job_id) or True
        if batch["status"] == "held" and batch.get("passed_over") and newer is None and record.get("status") != "continued":
            # The reply passed over for this turn: neither went out, said once.
            passed = batch["passed_over"]
            if self.update(ctx, batch, passed_over=None) is not None:
                self.note(ctx, settings, batch, f"⏸ 没有代你回复：#{passed} 的回复没有发（之后你在本话题又说了话），"
                                                f"#{job_id} 这一轮也没有要发给对方的回复。\n要回的话，在本话题 @机器人 告诉我怎么回。",
                          tag=f"passed-{job_id}")
        if batch["status"] != "held":
            # Nothing to tell the owner: continued (the model asked on a question card and was answered;
            # the follow-up task in the topic carries the reply), or stopped on its card by the owner.
            quiet = ("等提问后的后续任务" if record.get("status") == "continued" else
                     "你停止了任务" if record.get("status") == "cancelled" and record.get("cancel_requested_by") else "")
            # The model's own reason is in the turn's answer just above; said once there (repo review U5).
            said = reply is not None and bool(reason)
            suggestion = records.suggested_reply(reason) if said and not quiet else ""
            held = records.move_batch(ctx.state_dir, batch, "held", reason=quiet or reason or "模型判断不该代答",
                                      considered_job_id=job_id,
                                      **({"reply": suggestion, "reply_job_id": job_id} if suggestion else {}))
            if held is not None and not quiet and not (suggestion and self.resend.show(ctx, settings, held, "declined")):
                self.note(ctx, settings, batch, (f"⏸ 没有代你回复（原因见上面 #{job_id} 的回答）" if said else
                                                 f"⏸ 没有代你回复：{reason or '模型判断不该代答'}")
                          + "\n要回的话，在本话题 @机器人 告诉我怎么回。")
        return False

    @staticmethod
    def newer_batch(ctx, batch):
        """Whether the contact of ``batch`` has a batch taken in after it."""
        return any(b["contact"] == batch["contact"] and b["created_at"] > batch["created_at"]
                   for b in records.read_batches(ctx.state_dir))  # Open ones only.

    def send(self, ctx, settings, contact, batch, reply, job_id):
        """``reply`` (job ``job_id``'s) sent for ``batch``, or why not; LATER when HOURLY_LIMIT holds off the
        reply of a held batch (the owner's turn, a send tried again): said once, with when it goes out, and
        tried again by later polls; LATER too while the tool's login runs (repo review F2: the owner's turn
        was taken as done). Its at tags are plain text from here on (``plain_mentions``), as saved and read back."""
        reply = plain_mentions(reply)
        stop = self.stopped(ctx, batch, job_id)
        if stop:
            self.stop(ctx, settings, batch, job_id, *stop)
            return
        if self.web.owner_replied(ctx, settings, contact, batch):
            skipped = records.move_batch(ctx.state_dir, batch, "skipped", reason="你已亲自回复")
            self.tell(ctx, settings, batch, "dropped", "⏭ 你已经亲自回复了，这条不再代发。", shown=skipped)
            return
        sends = records.read_batches(ctx.state_dir, statuses=None)
        if records.recent_sends(sends, contact["key"], time.time()) >= records.HOURLY_LIMIT:
            if batch["status"] != "held":
                if records.move_batch(ctx.state_dir, batch, "held", reason="一小时内代答过多", reply=reply,
                                      reply_job_id=job_id) is not None:
                    self.note(ctx, settings, batch, f"⏸ 一小时内已给对方代答 {records.HOURLY_LIMIT} 条，暂停自动发送，请你自己看一下。")
                return
            if batch.get("later_job_id") != job_id and self.update(ctx, batch, later_job_id=job_id) is not None:
                at = time.localtime(records.next_send_at(sends, contact["key"], time.time()))
                self.note(ctx, settings, batch, f"⏸ 一小时内已给对方代答 {records.HOURLY_LIMIT} 条，"
                                                f"这条回复 {time.strftime('%H:%M', at)} 后自动发出。", tag=f"later-{job_id}")
            return LATER
        if not self.web_lock.acquire(blocking=False):
            # The tool's login is under way; the next poll sends.
            self.runtime.log(f"proxy batch={batch['id'][:8]} waits: the send tool's login is under way")
            return LATER
        try:
            with self.lock:
                # Checked again where 暂停, 删除 and 关闭 write: one from now on finds the batch sending.
                stop = self.stopped(ctx, batch, job_id)
                sending = None if stop else records.move_batch(
                    ctx.state_dir, batch, "sending", reply=reply, reply_job_id=job_id, send_started_at=time.time(),
                    attempts=int(batch.get("attempts") or 0) + 1)
            result = None if sending is None else self.web.web_send(ctx, settings, contact, sending, reply)
        finally:
            self.web_lock.release()
        if stop:
            self.stop(ctx, settings, batch, job_id, *stop)
        elif sending is not None:  # None: moved on meanwhile.
            self.finish_send(ctx, settings, contact, sending, result)

    def finish_send(self, ctx, settings, contact, batch, result):
        """What the send did. Certainly not sent (the tool's validation, unsupported, auth or failed, or it never
        started): the batch stays held: tried again up to SEND_ATTEMPTS, then its 重新发送 card offers to send
        the reply again, and the owner's next turn in the topic is sent too (张信宇 T190, 2026-10-27: a final
        ``failed`` left the owner's 重新回复 sending nothing); a lost login is also told to the owner once. Sent
        (the tool's receipt, read back by the tool): ``sent``, its om_ the receipt's, else read back. May have
        been (unknown_result, a timeout, a send the Bridge stopped in): a read-back finds it, else uncertain,
        never sent again. A batch with a card shows the outcome on it, and only there when the owner clicked
        重新发送 (they look at the card)."""
        if not result.get("ok") and not result.get("submitted"):
            error = result.get("error") or "发送失败"
            if result.get("type") == "auth":
                self.web_login.login_gone(ctx)
                if not self.web_login.login_seen or self.web_login.login_seen[0]:
                    self.web_login.status_next = 0.0  # The next poll's +status (unless it said logged out already).
            done = int(batch.get("attempts") or 0) >= records.SEND_ATTEMPTS
            held = records.move_batch(ctx.state_dir, batch, "held", reason=error, send_failed=True,
                                      **({"resend_requested": None, "relogin_at": None} if done else {}))
            if held is None or not done:  # None: moved on meanwhile.
                return
            # The card that was there (an owner's turn's reply failed too) is redrawn with this reply.
            card = batch.get("resend_card")
            if self.resend.show(ctx, settings, held, "again" if batch.get("resend_requested") else "failed"):
                if card and not batch.get("resend_requested") and not batch.get("relogin_at"):
                    # The redrawn card says why, once (UI review P11): the line only says it happened again.
                    self.note(ctx, settings, batch, "⚠️ 这一轮的回复也没发出，原因和要发的原文在上面的卡片上，可以点“重新发送”。")
                return
            who, chat = self.resend.names(ctx, batch)
            words, _ = records.unsent(error, contact.get("name") or chat, who)
            self.note(ctx, settings, batch, f"⚠️ 代答没有发出：{words}。\n"
                                      "要发的话，在本话题 @机器人 说“重新回复”，或直接说怎么回，那一轮的回复会发给对方"
                                      + ("；发送工具没登录的话，先点机器人菜单「代答」，在代答卡上点“网页端登录”扫码" if "登录" in error else "")
                                      + "。")
            return
        if result.get("ok"):
            self.web_login.login_back(ctx)
        if result.get("message_id"):
            sent = {"id": result["message_id"], "time": minute(result.get("create_time"))}
        elif result.get("ok"):
            # Sent, the tool read it back itself; without its om_ the owner's chat is read once, not waited for
            # (repo review F5: four reads 15 s apart held every batch up), and on later polls (``read_backs``). A
            # thread's reply is not in the chat's list: not read at all.
            thread = contact["kind"] == "group" and not records.listed(records.anchor(batch))
            sent = {} if thread else self.web.read_back(settings, contact, batch, tries=1)
            if not sent:
                sent = {"id": "", "time": minute(result.get("create_time"))}
                if not thread:
                    self.web.reads[batch["id"]] = READ_BACKS - 1
        else:
            sent = self.web.read_back(settings, contact, batch)
        if sent:
            moved = records.move_batch(ctx.state_dir, batch, "sent", sent_message_id=sent["id"])
            # The reply is the answer right above in the topic: the line records that it went out, and
            # when, without repeating it (UI review P8).
            self.tell(ctx, settings, batch, "sent", f"✅ 已代你回复（上一条，{clock(sent['time'])} 发出）", shown=moved)
        else:
            moved = records.move_batch(ctx.state_dir, batch, "uncertain", reason=result.get("error") or "读回时没找到")
            self.tell(ctx, settings, batch, "uncertain", "⚠️ 代答可能已发出但没能确认，不会自动重发，请到会话里看一眼：\n"
                      + batch["reply"], shown=moved)

    def stopped(self, ctx, batch, job_id):
        """Why a reply to ``batch`` (job ``job_id``'s) is not to go out now, from the records as they are
        now: (why, what to do first to have it sent), None to send it. 代答 off, its contact paused or
        removed, or the batch's own reply past STALE_AFTER; a later turn in the topic is the owner's, and
        so is a reply they asked to send again on its 重新发送 card."""
        if not records.read_settings(ctx.state_dir).get("enabled"):
            return "代答已关闭", "开启代答后"
        contact = next((c for c in records.read_contacts(ctx.state_dir) if c["key"] == batch["contact"]), None)
        if contact is None:
            return records.REMOVED, ""
        if contact.get("paused"):
            return f"已暂停对 {contact['name']} 的代答", "恢复代答后"
        newest = message_epoch(batch["messages"][-1]["time"]) or float(batch.get("created_at") or 0)
        age = time.time() - newest
        if job_id == batch.get("job_id") and age > records.STALE_AFTER and not batch.get("resend_requested"):
            return f"对方的消息已是 {age / 3600:.0f} 小时前的，这么晚的回复不自动发", ""
        return None

    def stop(self, ctx, settings, batch, job_id, why, first):
        """``batch`` not sent because ``why``: dropped for a removed contact, else held for the owner's
        next turn in the topic (once 代答 is on and the contact resumed: ``first``), and said there; a
        重新发送 asked for on the card is said on the card, whose button stays."""
        if why == records.REMOVED:
            self.resend.close(ctx, records.move_batch(ctx.state_dir, batch, "skipped", reason=why))
            return
        if batch["status"] == "held" and batch.get("resend_requested"):
            # Not tried again by itself: the card's button asks again.
            batch = self.update(ctx, batch, resend_requested=None, attempts=records.SEND_ATTEMPTS)
            if batch is None:
                return  # Moved on meanwhile.
            if self.resend.show(ctx, settings, batch, "failed", f"⚠️ 没有重新发送：{why}。{first}再点“重新发送”。"):
                self.runtime.log(f"proxy held batch={batch['id'][:8]} job=#{job_id}: {why} (on its card)")
                return
        if batch["status"] == "held":
            # The owner's turn, or another try of a send that never pressed Enter: no more tries by itself.
            moved = records.note_held(ctx.state_dir, batch, why, send_failed=False)
        else:
            moved = records.move_batch(ctx.state_dir, batch, "held", reason=why, considered_job_id=job_id)
        if moved is None:
            return  # Moved on meanwhile.
        self.runtime.log(f"proxy held batch={batch['id'][:8]} job=#{job_id}: {why}")
        self.note(ctx, settings, batch, f"⏸ 没有代你回复：{why}。\n要回的话，{first}在本话题 @机器人 告诉我怎么回。",
                  tag=f"stop-{job_id}")

    def tell(self, ctx, settings, batch, state, text, *, shown):
        """What became of ``batch`` (now ``shown``): on its 重新发送 card, drawn as ``state``, when it has
        one; as ``text`` in its topic unless the owner clicked that card and it showed it."""
        if shown is None:
            return  # Moved on meanwhile: said by what moved it.
        drawn = bool(batch.get("resend_card")) and self.resend.show(ctx, settings, shown, state) is not None
        if not (drawn and batch.get("resend_requested")):
            self.note(ctx, settings, batch, text)

    def note(self, ctx, settings, batch, text, *, tag=""):
        """A line in the batch's topic, under its messages; ``tag`` tells two lines of one state apart."""
        key = f"{batch['id']}-{len(batch.get('history', []))}" + (f"-{tag}" if tag else "")
        self.messenger.send_message(ctx.profile, settings["group_chat_id"], text, self.voice.key("proxy-note", key),
                                    reply_to_message_id=batch.get("mirror_id") or "", voice=self.voice)
