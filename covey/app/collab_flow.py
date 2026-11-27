"""协作的执行闭环 (docs/architecture.md 多参与者协作, COVEY_COLLAB=1; off: no rule line, no thread, no record, nothing
read): one participant asks others, each answers in a read-only fork of its own session (app/collab_channel.py), and the
asker, the integrator, goes on with the answers and delivers. The records are core/collab_records.py's, written by the
Bridge alone (rule 1: decided under the record's lock, sent outside it).

- Started: by the owner's own turn of a topic that wrote ``collab_ask`` ([{to, question}]) into its checkpoint (``RULE``,
  offered by ``turn_note`` in such a turn alone), read once its runner is gone (``Collaboration.after_runner``:
  app/detached.py collab_after), or by ``Collaboration.start`` (the API package E's page and cards call, the same checks and
  record). The root is the owner's message of that turn; ``collab_id`` of it, the integrator and the helpers makes a second
  submission find the first. The checks (app/collab_checks.py): the owner's private chat, the switch, the integrator's
  Harness, at most HELPERS_NAMED helpers he named (HELPERS_DEFAULT otherwise), each helper's static ones (an ask it fails
  is ``not_taken`` with the reason). The 协作卡 in the integrator's thread (app/collab_cards.py) says who was asked and who was not, and
  every change of the record redraws it; a line says it only when the card could not go out.
- Dispatched (``dispatch``: right away, and the ``collab`` thread every TICK seconds): a queued ask whose helper is idle
  is marked taken first, then its turn is queued in its channel (``enqueue_chat_job`` with the turn source ``collab_ask``,
  the message id the Bridge's own: core/collab.py ask_message); a busy helper keeps it queued (never a 补充 of its running
  task, the owner's queued messages first), one waiting for the owner's answer, never started or stopped by the owner
  makes it ``not_taken``. A clarification (number 2) goes on the first ask's fork when the helper ran nothing since;
  else the channel forks the helper anew, told what was asked and answered and how many turns ran since.
- Settled (``Collaboration.after_runner`` for an ask's turn): done with its authorized reply (rule 5, core/saved_results.py) is
  ``replied`` with the words and what they stand on; a try that failed or was interrupted is queued once more by the
  record (RETRIES, never by the topic's recovery card or automatic retry); else ``failed``. A ``replied`` ask puts one line
  in the helper's thread (已协助 T3) and, in a project, a ``collab_answered`` receipt in its lead's inbox (no wake-up).
- Continued (``advance``): once no ask is waiting, the integrator's turn is queued with the answers and the gaps (turn
  source ``collab_reply``; its asks become ``read``); it may clarify each helper once (``collab_ask`` again) and write
  ``collab_gap`` for what it cannot close (a ``collab_gap`` receipt wakes the lead). Its end with nothing waiting is
  ``done``. What the owner said in its topic since the root is said in that turn (``goal_note``): his newest words first.
- Expired: an ask queued QUEUE_SECONDS, or anything open at the window's end (WINDOW_SECONDS; both configurable:
  core/collab_records.py queue_seconds, window_seconds), is ``expired`` (a taken
  ask's turn stopped) and the integrator goes on with what it has; nothing the owner started is marked failed.
- Stopped (``Collaboration.stop``, and the owner stopping the integrator's continuation): ``cancelled``, its asks and
  continuation stopped, nothing else of the helpers touched; an answer that comes late is kept (``late``), wakes nobody.
- Restarted: the thread reconciles from the records: a taken ask whose turn ended is settled from its record, one never
  queued is queued again under the same message id; a model is never run again to redeliver anything.
"""
from __future__ import annotations

import time
from pathlib import Path

from covey.app import collab_channel, provenance
from covey.app.collab_cards import CollabCards
from covey.app import collab_checks as checks
from covey.core import collab, projects, turn_source
from covey.core import collab_records as records
from covey.core.butler import traits
from covey.core.checkpoint import read_checkpoint
from covey.core.history_storage import append_history
from covey.core.jobs import context_for_record, find_job_record, harness_of, job_key, topic_job_records
from covey.core.loops import every
from covey.core.saved_results import load_authorized_reply
from covey.core.state import read_object
from covey.core.statuses import ENDED
from covey.core.text import cut
from covey.core.topics import load_topic, remember_topic_message, topic_file

FIELD = "collab_ask"  # The checkpoint's asks: [{to, question}] (or one {to, question}).
GAP_FIELD = "collab_gap"  # A continuation's gap it cannot close, for the lead (one line).
TICK = 15
START_WAIT = 20  # After the Bridge starts: its own recovery of interrupted turns goes first.
LOST = 120  # Seconds a taken ask or a queued continuation may have no job record before it is queued again.
QUESTION_CHARS = 1500
GAP_CHARS = 300
PENDING = (collab.CHECKING, collab.QUEUED, collab.TAKEN)
ENDED_WELL = ("done", "waiting_user")

# Put after the checkpoint file in the owner's own turn of a topic that may ask (``turn_note``).
RULE = ("请别的话题帮忙：主人要你问问别的话题（他点了 T 号），或你需要另一个话题自己会话里的结论时，在检查点写 "
        "collab_ask：[{\"to\":\"T5\",\"question\":\"要它回答什么，背景写清楚\"}]（主人点名的最多 3 位，没点名的最多 2 位），"
        "回复里说去问了谁。本轮结束后 Bridge 让对方在它自己会话的只读分叉里回答（它的会话和手上的事都不动），"
        "答复齐了你接着做、把结论交给主人，写明哪些来自谁。能自己查到的不要问。")
REPLY_HEAD = ("[Bridge 协作答复] 你在任务 #{job} 里请别的参与者协助（协作 {short}），{why}。主人不在场：先核对答复，"
              "再接着做主人要的事，最后把结论交给主人，写明哪些来自谁、依据是什么。")
REPLY_TAIL = ("还要向同一位问清楚一次（每位最多再问一次），在检查点写 collab_ask：[{{\"to\":\"T5\",\"question\":\"…\"}}]。"
              "{gap}")
GAP_RULE = "有你自己补不上的缺口（缺材料、冲突、依赖别的话题），在检查点写 collab_gap：\"一句话\"，Bridge 会告诉负责人。"
GOAL_NOTE = ("注意：你发起这次协作之后，主人在这个话题里又说了话（{rows}）。以主人最新的要求为准：上面的答复是针对之前的问题的，"
             "只作参考，不能当成新要求的答案。")
START_LINE = "🤝 协作 {short}：{parts}。答复齐了 {who} 接着做。"
REFUSED_LINE = "⚠️ 没有发起协作：{reason}"
HELPED_LINE = "🤝 已协助 {asker}：{question}（只读咨询：本话题的会话、记录和手上的事都没有动）"
STOPPED_REASON = "主人停了这件事"
EXPIRED_REASON = "到期没有答复"
QUEUE_EXPIRED = "排队超过 {minutes} 分钟，对方一直在忙"
FAILED_REASON = "对方这一轮没有完成（{status}）"
NOT_QUEUED = "没能排上对方的通道"


def enabled() -> bool:
    return turn_source.enabled()


def wanted(cp) -> list:
    """The checkpoint's ``collab_ask`` as [{to, question}] (cut; entries without both left out); [] without one."""
    raw = cp.get(FIELD) if isinstance(cp, dict) else None
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    found = []
    for item in raw[:collab.HELPERS_NAMED + 2]:
        if not isinstance(item, dict):
            continue
        to = " ".join(str(item.get("to") or "").split())
        question = str(item.get("question") or item.get("text") or "").strip()
        if to and question:
            found.append({"to": to[:40], "question": question[:QUESTION_CHARS]})
    return found


def gap_of(cp) -> str:
    raw = cp.get(GAP_FIELD) if isinstance(cp, dict) else None
    return cut(" ".join(str(raw or "").split()), GAP_CHARS) if raw else ""


def direct(job, access) -> bool:
    """Whether ``job`` is the owner's own words to its topic (his Feishu message or the web page's, or his answer when it is
    stamped), of a source whose words are his (no hand-on, no turn of 协作), not an automatic retry, his by rule 11
    (app/provenance.py ``owner_said``)."""
    return provenance.owner_said(job, access)


def owner_rows(state, topic, chat, after) -> list:
    """The integrator's records of the owner's words (a source with them, his own message) newer than task ``after``."""
    rows = []
    for record in topic_job_records(Path(state), topic, chat_id=chat):
        if int(record.get("job_id") or 0) <= int(after or 0):
            continue
        if turn_source.behavior(record).owner_words and turn_source.level(record) == turn_source.LEVEL_OWNER:
            rows.append(record)
    return sorted(rows, key=lambda record: int(record.get("job_id") or 0))


def goal_note(ctx, job) -> str:
    """For an integrator's continuation (``collab_reply``): the owner's words in its topic since the collaboration began
    (``goal_rev``), newest goal first; '' when he said nothing since."""
    source = turn_source.of(job) or {}
    record = records.read(ctx.state_dir, str(source.get("collab") or ""))
    if not record:
        return ""
    later = owner_rows(ctx.state_dir, ctx.topic_id, str(getattr(job, "chat_id", "") or ""), record.get("goal_rev"))
    if not later:
        return ""
    rows = "；".join(f"任务 #{row.get('job_id')}：{cut(' '.join(str(row.get('user_text') or '').split()), 120)}"
                    for row in later[-3:])
    return "\n\n" + GOAL_NOTE.format(rows=rows)


def turn_note(access, harness, ctx, job) -> str:
    """What app/runs.py puts after the checkpoint file: ``RULE`` in the owner's own turn of a topic that may start a
    collaboration (a participant of the owner's private chat on a Harness that declares ``collab``), the owner's later
    words in an integrator's continuation (``goal_note``); '' otherwise and with the switch off."""
    if not enabled() or job is None:
        return ""
    if turn_source.kind(job) == turn_source.REPLY:
        return goal_note(ctx, job)
    from covey.core.harness import backend_capabilities

    chat = str(getattr(job, "chat_id", "") or "")
    topic = getattr(job, "topic_id", "") or ctx.topic_id
    if not direct(job, access) or not access.agent_chat(chat, group=False):
        return ""
    who = checks.participant(ctx.state_dir, chat, topic)
    if who is None or who.role != collab.TOPIC or not backend_capabilities(harness.backend, ctx).collab:
        return ""
    return "\n\n" + RULE


class Collaboration:
    """The feature object (app/unified.py PARTS ``collab``): its thread, none with the switch off, and the API.
    ``topics``: app/topics.py Topics (the Harness apps by name)."""

    name = "collab"

    def __init__(self, runtime, topics, messenger, voice) -> None:
        self.runtime, self.topics, self.messenger, self.voice = runtime, topics, messenger, voice
        self.cards = CollabCards(runtime, messenger, voice, self)  # 协作卡 (app/collab_cards.py): the record's projection in the chat.

    # -- the thread ----------------------------------------------------------------------------------------------------

    def threads(self):
        return ((self.name, self.run),) if enabled() else ()

    def run(self, ctx) -> None:
        if self.runtime.shutdown.wait(START_WAIT):
            return
        every(self.runtime, TICK, self.tick, args=(ctx,), isolate=self.name)

    def tick(self, ctx, now=None) -> None:
        """One look at every open collaboration (``look``)."""
        if not enabled():
            return
        now = time.time() if now is None else now
        for record in records.records(ctx.state_dir, open_only=True):
            try:
                self.look(ctx, record, now)
            except Exception as exc:  # noqa: BLE001 - the others are looked at all the same
                self.runtime.log(f"collab look failed collab={str(record.get('id'))[:8]}: {type(exc).__name__}: {str(exc)[:160]}")

    def look(self, ctx, record, now) -> None:
        cid, state = record["id"], ctx.state_dir
        if now >= float(record.get("deadline") or 0):
            self.expire(ctx, cid, now, window=True)
        record = records.read(state, cid)
        for ask in list(record.get("asks") or ()):
            if record.get("state") != collab.OPEN:
                break
            if ask.get("state") == collab.CHECKING:
                self.check(ctx, cid, ask["id"], now)
            elif ask.get("state") == collab.QUEUED:
                if now >= float(ask.get("deadline") or 0):
                    self.end_ask(ctx, cid, ask["id"], collab.EXPIRED,
                                 QUEUE_EXPIRED.format(minutes=max(1, records.queue_seconds() // 60)), now)
                else:
                    self.dispatch(ctx, cid, ask["id"], now)
            elif ask.get("state") == collab.TAKEN:
                job = find_job_record(Path(state), str(ask.get("message_id") or ""))
                if job is None and now - float(ask.get("taken_at") or 0) > LOST:
                    self.queue_ask(ctx, cid, ask["id"])  # Marked, never queued (the Bridge stopped in between).
                elif job is not None and job.get("status") in ENDED:
                    self.settle_ask(ctx, job, now)  # Ended while no Bridge saw its runner go.
        record = records.read(state, cid)
        for reply in record.get("replies") or ():
            if reply.get("state") != "queued":
                continue
            job = find_job_record(Path(state), str(reply.get("message_id") or ""))
            if job is None and now - float(reply.get("at") or 0) > LOST:
                self.queue_reply(ctx, cid, int(reply["number"]))
            elif job is not None and job.get("status") in ENDED:
                self.from_checkpoint(ctx, job, clarify=True)  # Its clarifications and gap, as after_runner would.
                self.settle_reply(ctx, job, now)
        self.advance(ctx, cid, now)
        self.cards.refresh(ctx, cid, create=False)  # A write that missed (digest differs), tried again.

    # -- the API (package E's page and cards; the checkpoint's asks come the same way) --------------------------------

    def start(self, ctx, chat, *, root, integrator, asks, owner_named=(), asserted_by=None, now=None) -> dict:
        """Start (or find) the collaboration of the owner's message ``root`` ({message_id, job_key, job_id, words}) with
        ``integrator`` (a topic id) asking each of ``asks`` ([{to: a topic id, question}]). ``owner_named``: the helpers he
        picked himself (his @, or his words naming them). ``asserted_by``: whose words the questions are
        (core/turn_source.py PARTIES; the integrator's role by default). {"collab": the record's view} or {"refused": why}."""
        now = time.time() if now is None else now
        state, access = ctx.state_dir, self.runtime.access
        who = checks.participant(state, chat, integrator)
        refused = checks.admission(access, self.topics, state, chat, who)
        if refused:
            return {"refused": refused}
        keys, questions = [], {}
        for item in asks:
            key = str(item.get("to") or "")
            if key and key not in questions:
                keys.append(key)
                questions[key] = str(item.get("question") or "")[:QUESTION_CHARS]
        keys = [key for key in keys if key != who.key] or keys
        named = set(owner_named)
        limit = collab.HELPERS_NAMED if keys and all(key in named for key in keys) else collab.HELPERS_DEFAULT
        if not keys:
            return {"refused": "没有要请的协助方"}
        if len(keys) > limit:
            return {"refused": collab.TOO_MANY.format(n=limit, m=len(keys))}
        cid = collab.collab_id(root["message_id"], who.key, keys)
        if records.read(state, cid):
            return {"collab": records.view(records.read(state, cid)), "again": True}
        rows = owner_rows(state, who.key, chat, 0)
        goal = max([int(root.get("job_id") or 0), *(int(row.get("job_id") or 0) for row in rows
                                                     if int(row.get("job_id") or 0) <= int(root.get("job_id") or 0))])
        party = asserted_by or {collab.BUTLER: turn_source.PARTY_BUTLER, collab.LEAD: turn_source.PARTY_LEAD}.get(
            who.role, turn_source.PARTY_TOPIC)
        source = turn_source.provenance(party, turn_source.PARTY_BRIDGE,
                                        {"message_id": root["message_id"], "topic": who.key, "chat": chat})
        integrator_row = records.participant(who.key, who.role, who.project, checks.label(state, chat, who))
        helper_rows, made = [], []
        for key in keys:
            helper = checks.participant(state, chat, key)
            shown = checks.label(state, chat, helper) if helper is not None else key[:8]
            helper_rows.append(records.participant(key, helper.role if helper else collab.TOPIC,
                                                   helper.project if helper else "", shown, key in named))
            ask = records.new_ask(cid, to=key, frm=who.key, number=1, text=questions[key],
                                  channel=collab.channel_id(cid, key), now=now)
            reason = checks.helper_refusal(self.topics, state, chat, who, key, owner_named=key in named)
            if reason:
                records.move_ask(ask, collab.NOT_TAKEN, now, reason=reason)
            else:
                records.move_ask(ask, collab.QUEUED, now, deadline=now + records.queue_seconds())
            made.append(ask)

        def create(current):
            if current:
                return None  # A second submission found the first.
            record = records.new_record(cid, chat, root={**root, "topic": who.key}, integrator=integrator_row,
                                        helpers=helper_rows, goal_rev=goal, source=source, now=now)
            record["asks"] = made
            return record
        written = records.change(state, cid, create)
        if written is None:
            return {"collab": records.view(records.read(state, cid)), "again": True}
        self.runtime.log(f"collab started collab={cid[:8]} integrator={who.key[:12]} helpers={len(keys)}")
        for ask in made:
            if ask["state"] == collab.QUEUED:
                self.dispatch(ctx, cid, ask["id"], now)
        record = records.read(state, cid)
        if not self.cards.refresh(ctx, cid):  # The card says what the line did; the line only when the card did not go out.
            self.tell_start(ctx, record)
        for ask in record.get("asks") or ():
            if ask.get("state") == collab.NOT_TAKEN:
                self.receipts(ctx, record, ask)
        self.advance(ctx, cid, now)
        self.cards.refresh(ctx, cid, create=False)
        return {"collab": records.view(records.read(state, cid))}

    def clarify(self, ctx, collab_id, helper, question, now=None) -> dict:
        """The integrator's one clarification (number 2) to ``helper`` (a topic id) of ``collab_id``: {"collab": view} or
        {"refused": why}. Only to a helper already asked, whose first ask is settled, once."""
        now = time.time() if now is None else now
        state = ctx.state_dir
        record = records.read(state, collab_id)
        if not record:
            return {"refused": "找不到这次协作"}
        if record.get("state") != collab.OPEN:
            return {"refused": "这次协作已经结束"}
        asked = records.asks_to(record, helper)
        if not asked:
            return {"refused": "协作进行中只能向已经请过的协助方澄清；要请新的人，等主人发话"}
        if any(int(ask.get("number") or 0) > 1 and ask.get("text") == str(question or "")[:records.TEXT_CHARS]
               for ask in asked):
            return {"collab": records.view(record), "again": True}  # The same continuation's end read twice.
        if len(asked) >= collab.ASKS_PER_HELPER:
            return {"refused": f"每位最多问 {collab.ASKS_PER_HELPER} 次，已经问满了"}
        if not records.settled(asked[-1]):
            return {"refused": "上一次还没有答复"}
        who = checks.participant(state, record["chat_id"], (record.get("integrator") or {}).get("key", ""))
        if who is None:
            return {"refused": checks.NOT_A_TOPIC}
        ask = records.new_ask(collab_id, to=helper, frm=who.key, number=len(asked) + 1, text=question,
                              channel=collab.channel_id(collab_id, helper), now=now)
        named = any(row.get("key") == helper and row.get("named") for row in record.get("helpers") or ())
        reason = checks.helper_refusal(self.topics, state, record["chat_id"], who, helper, owner_named=named)
        if reason:
            records.move_ask(ask, collab.NOT_TAKEN, now, reason=reason)
        else:
            records.move_ask(ask, collab.QUEUED, now, deadline=now + records.queue_seconds())

        def add(current):
            if current.get("state") != collab.OPEN or records.ask_of(current, ask["id"]):
                return None
            current["asks"] = [*(current.get("asks") or []), ask]
            return current
        if records.change(state, collab_id, add) is None:
            return {"collab": records.view(records.read(state, collab_id)), "again": True}
        if ask["state"] == collab.QUEUED:
            self.dispatch(ctx, collab_id, ask["id"], now)
        else:
            self.receipts(ctx, records.read(state, collab_id), ask)
        self.advance(ctx, collab_id, now)
        self.cards.refresh(ctx, collab_id, create=False)
        return {"collab": records.view(records.read(state, collab_id))}

    def stop(self, ctx, collab_id, by="card", now=None) -> dict:
        """The owner stopped the collaboration: ``cancelled``, its asks still waiting or running cancelled and their turns
        stopped, its continuation queued or running stopped; nothing else of any helper is touched. {"collab": view}."""
        now = time.time() if now is None else now
        state, stopping = ctx.state_dir, []

        def cancel(current):
            if current.get("state") != collab.OPEN:
                return None
            records.move_collab(current, collab.CANCELLED, now, stopped_by=str(by or "owner"))
            for ask in current.get("asks") or ():
                if ask.get("state") in PENDING:
                    if ask.get("state") == collab.TAKEN and ask.get("message_id"):
                        stopping.append((ask.get("channel"), ask["message_id"]))
                    records.move_ask(ask, collab.CANCELLED, now, reason=STOPPED_REASON)
            for reply in current.get("replies") or ():
                if reply.get("state") == "queued":
                    stopping.append(((current.get("integrator") or {}).get("key"), reply.get("message_id")))
            return current
        record = records.change(state, collab_id, cancel)
        if record is None:
            return {"collab": records.view(records.read(state, collab_id))}
        self.runtime.log(f"collab stopped collab={collab_id[:8]} by={by}")
        for topic, message in stopping:
            self.stop_job(ctx, record["chat_id"], topic, message, by)
        self.cards.refresh(ctx, collab_id, create=False)
        return {"collab": records.view(records.read(state, collab_id))}

    def view(self, ctx, collab_id) -> dict:
        return records.view(records.read(ctx.state_dir, collab_id))

    def of_chat(self, ctx, chat, *, open_only=False) -> list:
        return [records.view(record) for record in records.records(ctx.state_dir, chat=chat, open_only=open_only)]

    # -- asks ------------------------------------------------------------------------------------------------------------

    def check(self, ctx, cid, ask_id, now) -> None:
        """An ask left in CHECKING (the Bridge stopped while starting it): its static checks again."""
        record = records.read(ctx.state_dir, cid)
        who = checks.participant(ctx.state_dir, record["chat_id"], (record.get("integrator") or {}).get("key", ""))
        ask = records.ask_of(record, ask_id)
        named = any(row.get("key") == ask.get("to") and row.get("named") for row in record.get("helpers") or ())
        reason = (checks.NOT_A_TOPIC if who is None else
                  checks.helper_refusal(self.topics, ctx.state_dir, record["chat_id"], who, ask["to"], owner_named=named))

        def move(current):
            found = records.ask_of(current, ask_id)
            if found.get("state") != collab.CHECKING:
                return None
            if reason:
                records.move_ask(found, collab.NOT_TAKEN, now, reason=reason)
            else:
                records.move_ask(found, collab.QUEUED, now, deadline=now + records.queue_seconds())
            return current
        records.change(ctx.state_dir, cid, move)

    def dispatch(self, ctx, cid, ask_id, now=None) -> None:
        """A queued ask whose helper is idle: marked taken (its message id first), then its turn queued; a busy helper
        keeps it queued; anything else ends it not taken, with the reason."""
        now = time.time() if now is None else now
        state = ctx.state_dir
        record = records.read(state, cid)
        ask = records.ask_of(record, ask_id)
        if record.get("state") != collab.OPEN or ask.get("state") != collab.QUEUED:
            return
        found = checks.receiver_now(self.runtime, state, record["chat_id"], ask["to"])
        if found == checks.BUSY:
            return
        if found != checks.IDLE:
            self.end_ask(ctx, cid, ask_id, collab.NOT_TAKEN, found, now)
            return
        attempt = int(ask.get("attempt") or 0) + 1

        def take(current):
            mine = records.ask_of(current, ask_id)
            if current.get("state") != collab.OPEN or mine.get("state") != collab.QUEUED:
                return None
            base = checks.last_ended(state, current.get("chat_id", ""), mine.get("to", ""))
            records.move_ask(mine, collab.TAKEN, now, attempt=attempt, taken_at=now,
                             message_id=collab.ask_message(ask_id, attempt), job_key="",
                             # The helper as it was when taken; the turn's own record says what it forked (EVIDENCE).
                             base={"job_id": int(base.get("job_id") or 0), "prompt_uuid": str(base.get("prompt_uuid") or "")})
            return current
        if records.change(state, cid, take) is None:
            return
        self.queue_ask(ctx, cid, ask_id)
        self.cards.refresh(ctx, cid, create=False)

    def queue_ask(self, ctx, cid, ask_id) -> None:
        """The taken ask's turn queued in its channel under its message id (once: a job record of it already there is
        not queued again)."""
        state = ctx.state_dir
        record = records.read(state, cid)
        ask = records.ask_of(record, ask_id)
        message, helper = str(ask.get("message_id") or ""), ask.get("to", "")
        if record.get("state") != collab.OPEN or ask.get("state") != collab.TAKEN or not message:
            return
        if find_job_record(Path(state), message) is not None:
            return
        job = None
        try:
            job = self._queue_ask(ctx, record, ask)
        except Exception as exc:  # noqa: BLE001 - said in the record: the integrator goes on without it
            self.runtime.log(f"collab ask not queued ask={ask_id[-12:]}: {type(exc).__name__}: {str(exc)[:160]}")
        if job is None:
            self.end_ask(ctx, cid, ask_id, collab.FAILED, NOT_QUEUED)
            return

        def keep(current):
            mine = records.ask_of(current, ask_id)
            if mine.get("message_id") != message:
                return None
            mine["job_key"] = job_key(message, job.job_id)
            return current
        records.change(state, cid, keep)
        self.runtime.log(f"collab ask queued ask={ask_id[-12:]} helper={helper[:12]} message={message[-16:]}")

    def _queue_ask(self, ctx, record, ask):
        state, chat, helper = ctx.state_dir, record["chat_id"], ask["to"]
        data = load_topic(Path(state), helper)
        app = self.topics.apps.get(data.get("harness_id") or "")
        if app is None:
            return None
        integrator = (record.get("integrator") or {}).get("key", "")
        root = str(read_object(topic_file(Path(state), integrator)).get("root_id") or "")
        first = next((row for row in records.asks_to(record, helper) if int(row.get("number") or 0) == 1), {})
        again = None
        refresh = False
        if int(ask.get("number") or 1) > 1:
            since = int((first.get("evidence") or first.get("base") or {}).get("job_id") or 0)
            latest = int(checks.last_ended(state, chat, helper).get("job_id") or 0)
            if latest != since:
                turns = sum(1 for row in checks.ran(topic_job_records(Path(state), helper, chat_id=chat))
                            if int(row.get("job_id") or 0) > since and turn_source.behavior(row).own_delivery)
                again = {"asked": first.get("text"), "answered": (first.get("reply") or {}).get("text") or "（没有答复）",
                         "turns": turns}
                refresh = True
        channel = collab_channel.open_channel(state, chat, record, helper, root, refresh=refresh)
        if refresh:
            with self.runtime.session_lock:
                collab_channel.clear_pointer(app.harness, state, channel)
        who = checks.participant(state, chat, helper)
        view = collab_channel.read_only_view(ctx, chat, helper, who.role if who else collab.TOPIC, log=self.runtime.log)
        asker = (record.get("integrator") or {}).get("label") or "汇总方"
        text = collab_channel.ask_text(record, ask, asker, again=again, view=view)
        message = ask["message_id"]
        remember_topic_message(Path(state), channel, chat, message)
        target = app.submission.event_context(app.harness.context, {"chat_id": chat, "message_id": message})
        if target.topic_id != channel:
            return None
        owner = self.runtime.owner_open_id or ""
        source = turn_source.make(turn_source.ASK, asserted_by=(record.get("source") or {}).get("asserted_by")
                                  or turn_source.PARTY_TOPIC,
                                  origin={"topic": integrator, "chat": chat, **({"message_id": (record.get("root") or {})
                                          .get("message_id")} if (record.get("source") or {}).get("asserted_by")
                                          == turn_source.PARTY_OWNER else {})},
                                  collab=record["id"], ask=ask["id"])
        append_history(target.history_path, "user", text, {"message_id": message, "chat_id": chat})
        return app.submission.enqueue_chat_job(target, text, chat, message, prompt_content=text, sender_id=owner,
                                               source=source)

    def end_ask(self, ctx, cid, ask_id, new, reason, now=None) -> None:
        """``ask_id`` ended (not taken, failed, expired) with ``reason``, its receipts written; its turn stopped when it was
        taken (an expired one)."""
        now = time.time() if now is None else now
        ended = []

        def move(current):
            mine = records.ask_of(current, ask_id)
            if not mine or new not in collab.ASK_TRANSITIONS.get(mine.get("state"), ()):
                return None
            if mine.get("state") == collab.TAKEN and mine.get("message_id"):
                ended.append((mine.get("channel"), mine["message_id"]))
            records.move_ask(mine, new, now, reason=reason)
            return current
        record = records.change(ctx.state_dir, cid, move)
        if record is None:
            return
        for topic, message in ended:
            self.stop_job(ctx, record["chat_id"], topic, message, "card")
        self.receipts(ctx, record, records.ask_of(record, ask_id))
        self.cards.refresh(ctx, cid, create=False)

    def expire(self, ctx, cid, now, *, window=False) -> None:
        """The window's end: every ask still waiting or running expires (its turn stopped)."""
        record = records.read(ctx.state_dir, cid)
        for ask in record.get("asks") or ():
            if ask.get("state") in (collab.QUEUED, collab.TAKEN):
                self.end_ask(ctx, cid, ask["id"], collab.EXPIRED, EXPIRED_REASON, now)
            elif ask.get("state") == collab.CHECKING:
                self.end_ask(ctx, cid, ask["id"], collab.FAILED, EXPIRED_REASON, now)

    # -- settling --------------------------------------------------------------------------------------------------------

    def after_runner(self, job, ctx) -> None:
        """The Bridge, once ``job``'s runner is gone (app/detached.py collab_after: its finish_detached, and
        app/job_dispatch.py for a run here): an ask's turn settles its ask; an integrator's continuation is settled and may
        clarify; the owner's own turn of a topic may start a collaboration (``collab_ask`` in its checkpoint). Never
        raises; nothing with the switch off, and nothing in a task runner (app/detached.py after_job calls none there):
        only the Bridge writes a collaboration's record and queues its turns (a runner queues none)."""
        if not enabled() or self.runtime.in_runner:
            return
        try:
            self._after_runner(job, ctx)
        except Exception as exc:  # noqa: BLE001 - the task's end does not depend on it
            self.runtime.log(f"collab after job #{getattr(job, 'job_id', '?')} failed: {type(exc).__name__}: {str(exc)[:200]}")

    def _after_runner(self, job, ctx) -> None:
        state = Path(ctx.state_dir)
        record = find_job_record(state, str(getattr(job, "message_id", "") or ""))
        if not record:
            return
        kind = turn_source.kind(record)
        if kind == turn_source.ASK:
            self.settle_ask(ctx, record)
            return
        if kind == turn_source.REPLY:
            self.from_checkpoint(ctx, record, clarify=True)
            self.settle_reply(ctx, record)
            return
        if kind in (turn_source.LEGACY, turn_source.OWNER):
            self.from_checkpoint(ctx, record, clarify=False)

    def from_checkpoint(self, ctx, record, *, clarify) -> None:
        """The asks (and a continuation's gap) its checkpoint holds, once its turn ended well with its reply authorized."""
        path = str(record.get("checkpoint_path") or "")
        cp = read_checkpoint(Path(path)) if path else {}
        asks = wanted(cp)
        gap = gap_of(cp) if clarify else ""
        if not asks and not gap:
            return
        state, chat, topic = ctx.state_dir, str(record.get("chat_id") or ""), str(record.get("topic_id") or "")
        key = str(record.get("key") or "")
        if record.get("status") not in ENDED_WELL or not record.get("verified_reply_authorized"):
            self.runtime.log(f"collab ask ignored job=#{record.get('job_id')}: not ended well")
            return
        if clarify:
            source = turn_source.of(record) or {}
            cid = str(source.get("collab") or "")
            if gap:
                self.gap_receipt(ctx, records.read(state, cid), gap, key)
            for item in asks:
                helper = checks.resolve(state, chat, item["to"], topic)
                found = self.clarify(ctx, cid, helper, item["question"]) if helper else {"refused": checks.NOT_FOUND.format(name=item["to"])}
                if found.get("refused") and not found.get("again"):
                    self.runtime.log(f"collab clarification refused collab={cid[:8]}: {found['refused']}")
                    self.tell(ctx, chat, topic, REFUSED_LINE.format(reason=found["refused"]), f"{key}-{item['to']}")
            return
        if not direct(record, self.runtime.access):
            self.runtime.log(f"collab ask ignored job=#{record.get('job_id')}: not the owner's own turn")
            return
        words = owner_words(state, topic, record)
        resolved, unknown = [], []
        for item in asks:
            helper = checks.resolve(state, chat, item["to"], topic)
            (resolved if helper else unknown).append({**item, "to": helper or item["to"]})
        named = [item["to"] for item in resolved
                 if (who := checks.participant(state, chat, item["to"])) is not None and checks.named(words, state, chat, who)]
        if not resolved:
            self.tell(ctx, chat, topic, REFUSED_LINE.format(reason="；".join(
                checks.NOT_FOUND.format(name=item["to"]) for item in unknown) or "没有要请的协助方"), key)
            return
        root = {"message_id": str(record.get("message_id") or ""), "job_key": key, "job_id": int(record.get("job_id") or 0),
                "words": words}
        found = self.start(ctx, chat, root=root, integrator=topic, asks=resolved, owner_named=named)
        if found.get("refused"):
            self.tell(ctx, chat, topic, REFUSED_LINE.format(reason=found["refused"]), key)
        elif unknown:
            self.tell(ctx, chat, topic, REFUSED_LINE.format(reason="；".join(
                checks.NOT_FOUND.format(name=item["to"]) for item in unknown)), key + "-unknown")

    def settle_ask(self, ctx, job, now=None) -> None:
        """An ask's turn ended (``job`` its record): replied with its authorized reply, queued once more after a failure
        that may pass, or failed; a late answer of an ask that ended is kept, waking nobody."""
        now = time.time() if now is None else now
        state = ctx.state_dir
        source = turn_source.of(job) or {}
        cid, ask_id = str(source.get("collab") or ""), str(source.get("ask") or "")
        if job.get("status") not in ENDED:
            return
        text = self.reply_text(ctx, job)
        evidence = job.get(collab_channel.EVIDENCE) if isinstance(job.get(collab_channel.EVIDENCE), dict) else {}
        outcome = {}

        def move(current):
            mine = records.ask_of(current, ask_id)
            if not mine or mine.get("message_id") != job.get("message_id"):
                return None
            if records.ended(mine) or current.get("state") != collab.OPEN:
                if text and not mine.get("late"):
                    mine["late"] = {"text": cut(text, records.REPLY_CHARS), "at": now, "job_id": job.get("job_id")}
                    outcome["late"] = True
                    return current
                return None
            if mine.get("state") != collab.TAKEN:
                return None
            if text:
                changed = sum(1 for row in owner_rows(state, (current.get("integrator") or {}).get("key", ""),
                                                      current.get("chat_id", ""), current.get("goal_rev")))
                records.move_ask(mine, collab.REPLIED, now, evidence=evidence,
                                 reply={"text": cut(text, records.REPLY_CHARS), "at": now, "job_id": job.get("job_id"),
                                        "goal_rev": current.get("goal_rev"), "owner_since": changed,
                                        "evidence": evidence})
                outcome["replied"] = True
            elif self.may_retry(job, mine, current, now):
                attempt = int(mine.get("attempt") or 1) + 1
                mine.update(attempt=attempt, message_id=collab.ask_message(ask_id, attempt), job_key="", taken_at=now,
                            updated_at=now)
                outcome["retry"] = True
            else:
                records.move_ask(mine, collab.FAILED, now, reason=FAILED_REASON.format(status=job.get("status")))
                outcome["failed"] = True
            return current
        record = records.change(state, cid, move)
        if record is None:
            return
        if outcome.get("late"):
            self.runtime.log(f"collab late answer kept ask={ask_id[-12:]}")
            self.cards.refresh(ctx, cid, create=False)  # Its note: kept, and it went to nobody.
            return
        if outcome.get("retry"):
            self.runtime.log(f"collab ask queued again ask={ask_id[-12:]}")
            self.queue_ask(ctx, cid, ask_id)
            return
        ask = records.ask_of(record, ask_id)
        if outcome.get("replied"):
            self.helped(ctx, record, ask)
        self.receipts(ctx, record, ask)
        self.cards.refresh(ctx, cid, create=False)
        self.advance(ctx, cid, now)

    @staticmethod
    def may_retry(job, ask, record, now) -> bool:
        """Whether an ask's try that ended without a reply is queued once more: failed or interrupted (not stopped, not a
        safety refusal), within RETRIES and the window."""
        return (job.get("status") in ("failed", "interrupted") and not job.get("cancel_requested_by")
                and not job.get("policy_stop") and int(ask.get("attempt") or 1) <= collab.RETRIES
                and now < float(record.get("deadline") or 0))

    def reply_text(self, ctx, job) -> str:
        """The ask's turn's saved, authorized reply (rule 5: core/saved_results.py), '' when there is none to use."""
        if job.get("status") != "done":
            return ""
        app = self.topics.apps.get(harness_of(job, ""))
        if app is None:
            return ""
        try:
            target = context_for_record(app.harness.context, job, reply_to="")
            reply, _ = load_authorized_reply(target, job, app.harness.spec.name, str(job.get("chat_id") or ""))
        except (OSError, ValueError, KeyError) as exc:
            self.runtime.log(f"collab reply unusable job=#{job.get('job_id')}: {type(exc).__name__}")
            return ""
        return reply.text

    # -- the integrator's continuation ---------------------------------------------------------------------------------

    def advance(self, ctx, cid, now=None) -> None:
        """Once no ask waits: the integrator's next continuation with the asks it has not had (answers and gaps)."""
        now = time.time() if now is None else now
        state, number = ctx.state_dir, []

        def plan(current):
            if current.get("state") != collab.OPEN:
                return None
            asks = current.get("asks") or []
            if any(ask.get("state") in PENDING for ask in asks):
                return None
            fresh = [ask for ask in asks if records.settled(ask) and not ask.get("delivered_in")]
            if not fresh:
                return None
            if any(reply.get("state") == "queued" for reply in current.get("replies") or ()):
                return None  # One continuation at a time: the next takes what came meanwhile.
            n = len(current.get("replies") or ()) + 1
            for ask in fresh:
                ask["delivered_in"] = n
                if ask.get("state") == collab.REPLIED:
                    records.move_ask(ask, collab.READ, now)
            current["replies"] = [*(current.get("replies") or []),
                                  {"number": n, "message_id": collab.reply_message(cid, n), "state": "queued", "at": now,
                                   "asks": [ask["id"] for ask in fresh]}]
            number.append(n)
            return current
        if records.change(state, cid, plan) is None:
            return
        self.queue_reply(ctx, cid, number[0])
        self.cards.refresh(ctx, cid, create=False)

    def queue_reply(self, ctx, cid, number) -> None:
        """Continuation ``number`` queued in the integrator's topic (once: a job record of it is not queued again)."""
        state = ctx.state_dir
        record = records.read(state, cid)
        reply = next((row for row in record.get("replies") or () if int(row.get("number") or 0) == number), {})
        message = str(reply.get("message_id") or "")
        if record.get("state") != collab.OPEN or reply.get("state") != "queued" or not message:
            return
        if find_job_record(Path(state), message) is not None:
            return
        job = None
        try:
            job = self._queue_reply(ctx, record, reply)
        except Exception as exc:  # noqa: BLE001 - the thread tries again (LOST)
            self.runtime.log(f"collab continuation not queued collab={cid[:8]}: {type(exc).__name__}: {str(exc)[:160]}")
        self.runtime.log(f"collab continuation {'queued' if job is not None else 'not queued'} collab={cid[:8]} n={number}")

    def _queue_reply(self, ctx, record, reply):
        state, chat = ctx.state_dir, record["chat_id"]
        integrator = (record.get("integrator") or {}).get("key", "")
        data = load_topic(Path(state), integrator)
        app = self.topics.apps.get(data.get("harness_id") or "")
        if app is None:
            return None
        message = reply["message_id"]
        text = self.reply_message_text(record, reply)
        remember_topic_message(Path(state), integrator, chat, message)
        target = app.submission.event_context(app.harness.context, {"chat_id": chat, "message_id": message})
        if target.topic_id != integrator:
            return None
        source = turn_source.make(turn_source.REPLY, asserted_by=turn_source.PARTY_BRIDGE,
                                  origin={"topic": integrator, "chat": chat}, collab=record["id"])
        append_history(target.history_path, "user", text, {"message_id": message, "chat_id": chat})
        return app.submission.enqueue_chat_job(target, text, chat, message, prompt_content=text,
                                               sender_id=self.runtime.owner_open_id or "", source=source)

    def reply_message_text(self, record, reply) -> str:
        """What the integrator's continuation reads: why now, the owner's words of the root, each ask it has not had."""
        mine = [ask for ask in record.get("asks") or () if ask.get("id") in (reply.get("asks") or ())]
        labels = {row.get("key"): row.get("label") or str(row.get("key"))[:8] for row in record.get("helpers") or ()}
        answered = sum(1 for ask in mine if ask.get("state") in (collab.REPLIED, collab.READ))
        why = ("答复齐了" if answered == len(mine) else f"{answered} 位答复了，其余没有答复（原因见下）")
        root = record.get("root") or {}
        rows = [REPLY_HEAD.format(job=root.get("job_id") or "?", short=record["id"][:8], why=why)]
        if root.get("words"):
            rows.append(f"主人的原话（任务 #{root.get('job_id')}）：{root['words']}")
        for ask in mine:
            who = labels.get(ask.get("to"), str(ask.get("to"))[:8])
            which = "澄清" if int(ask.get("number") or 1) > 1 else "咨询"
            if ask.get("state") in (collab.REPLIED, collab.READ):
                evidence = ask.get("evidence") or {}
                level = collab.EVIDENCE_WORDS.get(evidence.get("level"), "")
                since = f"，截至它的任务 #{evidence['job_id']}" if evidence.get("job_id") else ""
                rows.append(f"- {who} 的{which}答复（{level}{since}）：\n{(ask.get('reply') or {}).get('text') or ''}")
            else:
                rows.append(f"- {who} 的{which}{collab.ASK_WORDS.get(ask.get('state'), '')}：{ask.get('reason') or ''}")
        in_project = bool((record.get("integrator") or {}).get("project"))
        rows.append(REPLY_TAIL.format(gap=GAP_RULE if in_project else ""))
        return "\n\n".join(rows)

    def settle_reply(self, ctx, job, now=None) -> None:
        """The integrator's continuation ended: its row says how; the owner stopping it stops the collaboration;
        nothing waiting and nothing new: ``done``."""
        now = time.time() if now is None else now
        state = ctx.state_dir
        source = turn_source.of(job) or {}
        cid = str(source.get("collab") or "")
        status = str(job.get("status") or "")
        if status not in ENDED and status != "waiting_user":
            return
        stopped = status == "cancelled" and str(job.get("cancel_requested_by") or "card") in ("card", "word", "web")

        def move(current):
            if current.get("state") != collab.OPEN:
                return None
            replies = current.get("replies") or []
            mine = next((row for row in replies if row.get("message_id") == job.get("message_id")), None)
            if mine is None and replies:
                mine = replies[-1]  # A retry of the continuation (its card's 检查并继续): the same turn's end.
            if mine is not None:
                mine.update(state=status, ended_at=now, job_key=str(job.get("key") or ""))
            if stopped:
                records.move_collab(current, collab.CANCELLED, now, stopped_by="owner")
                for ask in current.get("asks") or ():
                    if ask.get("state") in PENDING:
                        records.move_ask(ask, collab.CANCELLED, now, reason=STOPPED_REASON)
                return current
            waiting = any(ask.get("state") in PENDING or (records.settled(ask) and not ask.get("delivered_in"))
                          for ask in current.get("asks") or ())
            if not waiting and status in ENDED_WELL:
                records.move_collab(current, collab.DONE, now)
            return current
        record = records.change(state, cid, move)
        if record is not None and record.get("state") != collab.OPEN:
            self.runtime.log(f"collab {record['state']} collab={cid[:8]}")
        if record is not None:
            self.cards.refresh(ctx, cid, create=False)
        if record is not None and record.get("state") == collab.OPEN:
            self.advance(ctx, cid, now)

    # -- what is said and stopped ----------------------------------------------------------------------------------------

    def stop_job(self, ctx, chat, topic, message, by) -> None:
        """Stop the job of ``message`` in ``topic`` if it is queued or running, as its card's 停止 would
        (app/project_clicks.py stop_task: a live task runner is told, else the Bridge cancels it)."""
        from covey.app.project_clicks import stop_task
        from covey.core.jobs import STOP_SOURCES, harness_of

        if not topic or not message:
            return
        for job in self.runtime.jobs.open_in_topic(topic, chat):
            if job.message_id != message:
                continue
            app = self.topics.apps.get(harness_of(job, ""))
            if app is None:
                return
            try:
                stop_task(app.controls, app.harness.context, topic, chat, job, f"collab-stop-{message}",
                          by if by in STOP_SOURCES else "card")
            except Exception as exc:  # noqa: BLE001 - its own end settles it
                self.runtime.log(f"collab job not stopped message={message[-16:]}: {type(exc).__name__}")
            return

    def thread_root(self, ctx, chat, topic) -> str:
        """The message that opens ``topic``'s thread, '' for a place at the chat's top level (the butler's): where a line or the
        协作卡 of the topic goes."""
        root = "" if traits(topic, chat).top_level else str(read_object(topic_file(Path(ctx.state_dir), topic)).get("root_id") or "")
        return "" if collab.own_message(root) else root

    def tell(self, ctx, chat, topic, text, key) -> None:
        """A line in ``topic``'s thread (at the top level for the butler's): best effort, once a ``key``."""
        try:
            root = self.thread_root(ctx, chat, topic)
            self.messenger.send_message(ctx.profile, chat, text, self.voice.key("collab", key), voice=self.voice,
                                        reply_to_message_id=root)
        except Exception as exc:  # noqa: BLE001 - the record says it all the same
            self.runtime.log(f"collab line not sent: {type(exc).__name__}")

    def tell_start(self, ctx, record) -> None:
        labels = {row.get("key"): row.get("label") or str(row.get("key"))[:8] for row in record.get("helpers") or ()}
        parts = []
        for ask in record.get("asks") or ():
            who = labels.get(ask.get("to"), "")
            if ask.get("state") == collab.NOT_TAKEN:
                parts.append(f"{who} 未接手（{ask.get('reason')}）")
            elif ask.get("state") == collab.TAKEN:
                parts.append(f"已请 {who} 协助")
            else:
                parts.append(f"已请 {who} 协助（等它手上的事做完）")
        integrator = record.get("integrator") or {}
        self.tell(ctx, record["chat_id"], integrator.get("key", ""),
                  START_LINE.format(short=record["id"][:8], parts="；".join(parts), who=integrator.get("label") or "汇总方"),
                  f"start-{record['id']}")

    def helped(self, ctx, record, ask) -> None:
        """已协助 T3: one line in an ordinary helper's thread (the butler and a lead have none)."""
        helper = ask.get("to", "")
        if not traits(helper, record.get("chat_id")).has_thread:
            return
        asker = (record.get("integrator") or {}).get("label") or "汇总方"
        self.tell(ctx, record["chat_id"], helper,
                  HELPED_LINE.format(asker=asker, question=cut(" ".join(str(ask.get("text") or "").split()), 60)),
                  f"helped-{ask['id']}")

    def receipts(self, ctx, record, ask) -> None:
        """A settled ask in a project: a ``collab_answered`` receipt (no wake-up, core/collab.py RECEIPT_WAKES) in the
        inbox of the integrator's project and, when the helper is in another, of the helper's."""
        if not projects.enabled() or not ask:
            return
        from covey.app import project_lead

        integrator = record.get("integrator") or {}
        helper = next((row for row in record.get("helpers") or () if row.get("key") == ask.get("to")), {})
        words = collab.ASK_WORDS.get(ask.get("state"), "")
        said = cut(" ".join(str((ask.get("reply") or {}).get("text") or ask.get("reason") or "").split()), 160)
        text = f"{integrator.get('label') or '汇总方'} 请 {helper.get('label') or '协助方'} 协助：{words}" + (f"：{said}" if said else "")
        for project in dict.fromkeys(filter(None, (integrator.get("project"), helper.get("project")))):
            try:
                project_lead.add_receipt(ctx.state_dir, project, collab.RECEIPT_ANSWERED, text,
                                         thread=integrator.get("key", ""))
            except OSError as exc:
                self.runtime.log(f"collab receipt not written: {type(exc).__name__}")

    def gap_receipt(self, ctx, record, gap, key) -> None:
        """The integrator's ``collab_gap``: a receipt that wakes its project's lead (core/collab.py RECEIPT_WAKES), once a
        continuation (its job ``key`` kept in the record's ``gaps``, the end read twice writes one)."""
        project = (record.get("integrator") or {}).get("project") if record else ""
        if not projects.enabled() or not project:
            return
        from covey.app import project_lead

        def mark(current):
            if not current or key in (current.get("gaps") or ()):
                return None
            current["gaps"] = [*(current.get("gaps") or []), key]
            return current
        if records.change(ctx.state_dir, record["id"], mark) is None:
            return

        try:
            project_lead.add_receipt(ctx.state_dir, project, collab.RECEIPT_GAP,
                                     f"{(record.get('integrator') or {}).get('label') or '汇总方'} 协作里补不上的缺口：{gap}",
                                     thread=(record.get("integrator") or {}).get("key", ""))
        except OSError as exc:
            self.runtime.log(f"collab gap receipt not written: {type(exc).__name__}")


def owner_words(state, topic, record) -> str:
    """The owner's words of ``record``'s turn as the Bridge took them in (app/butler_asks.py owner_words)."""
    from covey.app.butler_asks import owner_words as words

    return words(state, topic, str(record.get("message_id") or ""), str(record.get("key") or ""))
