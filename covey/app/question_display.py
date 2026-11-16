"""Where a question is shown: its element on the task card, its standalone card (whose address
survives task completion and restart), the notice for an answer not submitted yet, and the
continuation's state on the card. ``question_card_lock`` orders one question's publishing and repainting.

A 话题区's question (a topic the butler opened, with COVEY_BUTLER=1) is also shown at the private
chat's top level (``mirror``; the owner, 2026-11-07: 两处都放): the same form, quoting the butler's card, with
what the turn wrote before it asked above it; one longer than MIRROR_OUTPUT_BYTES goes as its own message first.
Its record keeps mirror_message_id, mirror_card_id and mirror_output; every repaint of the question repaints it
too, so answering either folds both. Sent once (mirror_state creating, then sent or uncertain: rule 1). When the
asking turn's answer card in the topic shows 产物 (app/answer_cards.py holds_files), the copy shows the same panel
(``copy_files``, drawn from the upload's ledger each time) and is listed with the answer cards (register_copy): the
upload's end and a 重新上传 repaint it whole (``repaint_copy``, app/artifacts.py refresh_answer_cards)."""

from __future__ import annotations

import dataclasses
import json
import time
from pathlib import Path

from covey.core.card_sequence import file_card, reserve
from covey.core.locks import resource_lock
from covey.cards.elements import card, md, note
from covey.cards.question_cards import answer_notice_card, mirror_card, question_card, question_element, triage_element
from covey.core import butler, placement, triage
from covey.core.jobs import find_job_record
from covey.core.state import write_private_text
from covey.core.store import update_question
from covey.core.topics import route_message, topic_label

MIRROR_OUTPUT_BYTES = 12000  # A longer output goes as its own message before the top-level question card.


def standalone_card(ctx, record):
    """The standalone card of ``record``, its topic named on it (T191 · #1084); drawn as the top-level copy when it
    is at the top level itself (``at_top``)."""
    if record.get("at_top"):
        return top_card(ctx, record)
    return question_card(record, topic_label(ctx.state_dir, record["topic_id"]))


def top_card(ctx, record, files=None):
    """The top-level copy of ``record``'s card (``mirror``), its "你问" block as it was sent; ``files``: the asking
    turn's 产物 panel (QuestionDisplay.copy_files)."""
    return mirror_card(record, topic_label(ctx.state_dir, record["topic_id"]), record.get("mirror_output") or "",
                       ask=record.get("mirror_ask"), files=files)


def question_card_lock(path):
    """Orders the publishing and repainting of one question's card, in this process. It is held
    across the Feishu call; the cross-process record lock is taken inside it only to read and write."""
    return resource_lock('question-card', str(Path(path).absolute()))


class QuestionDisplay:
    """A Harness's question cards. Questions methods it uses are called through ``questions`` (the
    facade), so an override on the Questions instance still intercepts them."""

    def __init__(self, harness, *, runtime, messenger, cards, questions) -> None:
        self.harness = harness
        self.runtime = runtime
        self.messenger = messenger
        self.voice = harness.voice
        self.cards = cards
        self.questions = questions

    def commit_display(self, ctx, path, shown, **fields):
        """Record whether the form went out. A question cancelled or answered while it was being
        sent skipped its repaint (the form was not shown yet), so it is repainted now."""
        record = update_question(path, lambda current: {**current, "display_pending": not shown,
                                                        **(fields if shown else {})})
        if shown and record.get("status") != "pending":
            self.questions.paint_question_state(ctx, record)
        return record

    def paint(self, ctx, record):
        if record.get("display_pending") or (record.get("body_delivery_required")
                and not self.questions.checkpoints.result_delivered(ctx, record)):
            return
        try:
            if record.get("question_message_id"):
                self.questions.paint_standalone_question(ctx, record)
                self.paint_mirror(ctx, record)
                return
            with self.runtime.jobs.lock:
                job = self.runtime.jobs.by_id.get(int(record["job_id"]))
                if job and (job.topic_id != ctx.topic_id or job.chat_id != record["chat_id"]):
                    job = None
            if job:
                self.cards.set_task_element(ctx, job, question_element(record))
            elif record.get("card_id"):
                original = self.harness.topic_record(ctx, record["chat_id"], record["job_id"])
                if original:
                    self.cards.update_record_panel(ctx, record["chat_id"], "answer-state-" + str(time.time_ns()),
                        {"message_id": original.get("card_message_id", "")}, original, question_element(record))
            self.paint_mirror(ctx, record)
        except Exception as exc:  # noqa: BLE001 - answer delivery is independent from UI
            self.runtime.log(f"Answer state display failed: {exc}")

    def paint_task_card(self, ctx, record):
        """The question's element on its task's card, drawn as the record says now (the task's live card, or the record's card
        when the task is gone): what a question the lead held shows once it is the owner's (app/triage.py). Raises on a card
        that cannot be written; the caller says so in the log."""
        with self.runtime.jobs.lock:
            job = self.runtime.jobs.by_id.get(int(record["job_id"]))
            if job and (job.topic_id != ctx.topic_id or job.chat_id != record["chat_id"]):
                job = None
        element = triage_element(record) if triage.state(record) == triage.ESCALATED and record.get("status") == "pending" else question_element(record)
        if job:
            self.cards.set_task_element(ctx, job, element)
        elif record.get("card_id"):
            original = self.harness.topic_record(ctx, record["chat_id"], record["job_id"])
            if original:
                self.cards.update_record_panel(ctx, record["chat_id"], "answer-state-" + str(time.time_ns()),
                    {"message_id": original.get("card_message_id", "")}, original, element)

    @staticmethod
    def _asked_target(ctx, job, target):
        """Whose words and link the copy's "你问" block has: those of the request the butler started or handed
        ``job`` on with (app/relay_targets.py relay_target), as its relay's block has, the second topic of one message
        linking to its own root (S6); else the latest request on the topic (``target``, kind "follow": a turn the
        owner started there, its own words)."""
        from covey.app.relay_targets import relay_target

        try:
            return relay_target(ctx.state_dir, job) or target
        except Exception:  # noqa: BLE001 - the block as for a turn the owner started
            return target

    # The top-level copy of a 话题区's question.
    def mirror(self, ctx, record, output="", job=None):
        """Show the pending question ``record`` at the top level too, once, when its topic is a 话题区, or the butler's
        , whose card is in the owner's message's thread (core/placement.py question_copy); its message id,
        '' when it is not shown there (another topic, no butler request it came from, already tried). Threaded,
        it is a message of its own with a "你问" block (``job``: the turn that asks), quoting no card. Each try is one
        log line ``main relay state=… outcome=question``, as a relay's (app/relay_send.py send_once)."""
        from covey.app.relay_targets import area_target, ask_for

        chat, token = record.get("chat_id") or "", record.get("token") or ""
        place = placement.plan(ctx.state_dir, {"topic_id": record.get("topic_id"), "chat_id": chat})
        if (not place.row.question_copy or record.get("status") != "pending" or record.get("display_pending")):
            return ""
        target = (area_target(ctx.state_dir, record["topic_id"], chat) if place.traits.relays
                  else {"chat_id": chat, "reply_to": "", "label": "", "kind": "main"})
        if not target:
            return ""
        ask = (ask_for(ctx.state_dir, job, self._asked_target(ctx, job, target) if place.traits.relays else target,
                       ctx.profile, at=str(record.get("question_message_id") or ""), log=self.runtime.log)
               if place.row.ask_block and job is not None else None)
        reply_to = "" if place.row.question_copy == placement.TOP else target["reply_to"]
        path = ctx.state_dir / "questions" / (token + ".json")
        text = str(output or "").strip()
        apart = len(text.encode("utf-8")) > MIRROR_OUTPUT_BYTES
        with question_card_lock(path):
            current = update_question(path, lambda r: None if r.get("mirror_state") else
                                      {**r, "mirror_state": "creating", "mirror_output": "" if apart else text,
                                       **({"mirror_ask": ask} if ask else {})})
            if current is None:
                return ""
            files, drawn = self.copy_files(ctx, current)
            top = dataclasses.replace(ctx, reply_to_message_id=reply_to)
            label = target.get("label") or topic_label(ctx.state_dir, record["topic_id"])
            if apart:
                self.messenger.send_message(ctx.profile, chat, text, self.voice.key("question-top-output", token),
                                            reply_to_message_id=reply_to, voice=self.voice,
                                            card=card([md(text), note(f"{label} 这一轮的输出，问题在下一条")],
                                                      f"{label} · 这一轮的输出"))
            sent = []

            def remember(mid):
                sent.append(mid)
                mapping = ctx.state_dir / "browse-cards" / (self.voice.safe_key(mid) + ".json")
                saved = json.loads(mapping.read_text())
                saved["question_token"] = token
                write_private_text(mapping, json.dumps(saved))
                update_question(path, lambda r: {**r, "mirror_message_id": mid, "mirror_card_id": saved["card_id"]})
                # A quote of it goes to the topic; a reply to it stays where the topic's replies go.
                route_message(ctx.state_dir, chat, mid, record["topic_id"], holder=record["topic_id"])

            try:
                self.messenger.publish_card(top, chat, "question-top-" + token, top_card(ctx, current, files),
                                            on_sent=remember, voice=self.voice)
            except Exception as exc:  # noqa: BLE001 - as unconfirmed: never sent again
                self.runtime.log(f"question top-level copy failed token={token[:10]}: {type(exc).__name__}")
            latest = update_question(path, lambda r: {**r, "mirror_state": "sent" if sent else "uncertain"})
            if sent and files is not None:
                self.listed_copy(ctx, latest or current, drawn)
        self.runtime.log(f"main relay state={'sent' if sent else 'uncertain'} outcome=question job=#{record.get('job_id')}"
                         f" kind={target.get('kind')} token={token[:10]} message={(sent[0] if sent else '')[-8:]}")
        return sent[0] if sent else ""

    def paint_mirror(self, ctx, record):
        """Repaint the top-level copy of ``record``'s card as the card in the topic is: answering either folds both.
        Its 产物 as the upload stands now (``copy_files``); whether it was written."""
        mid = record.get("mirror_message_id")
        if not mid:
            return False
        path = ctx.state_dir / "browse-cards" / (self.voice.safe_key(mid) + ".json")
        files = drawn = None
        try:
            with resource_lock("card", record.get("mirror_card_id") or mid):
                # Read under the card's lock: a repaint racing the upload's end (repaint_copy) goes first or waits,
                # so the last write shows the ledger as it ends, never 上传中 after the links.
                files, drawn = self.copy_files(ctx, record)
                saved = json.loads(path.read_text())
                if saved.get("question_token") != record.get("token"):
                    raise ValueError("问题卡片归属不一致")
                sequence = reserve(file_card(path, saved))
                result = self.messenger.put_card(ctx.profile, saved["card_id"], top_card(ctx, record, files),
                                                 sequence=sequence, key=f"question-top-{record['token']}-{sequence}")
            if result is None:
                raise RuntimeError("顶层问题卡更新未确认")
        except Exception as exc:  # noqa: BLE001 - the card in the topic is the record's
            self.runtime.log(f"question top-level copy repaint failed: {type(exc).__name__}: {exc}")
            return False
        if files is not None:
            self.listed_copy(ctx, record, drawn, check=False)
        return True

    def copy_card(self, ctx, record):
        """``record``'s top-level copy as it is drawn everywhere (a send, a repaint, a click redrawing it): with its
        turn's 产物 (``copy_files``)."""
        return top_card(ctx, record, self.copy_files(ctx, record)[0])

    def copy_files(self, ctx, record):
        """(the 产物 panel, the upload's status it shows) for ``record``'s top-level copy: the asking turn's
        (``job_key``), as its answer card in the topic shows them (app/artifacts.py answer_files), only when that
        card shows them (app/answer_cards.py holds_files: not a turn still running, whose question asks mid-turn;
        a native question asked mid-turn gets them on a repaint once its turn's answer card is out); (None, None)
        otherwise, for a question at the top level itself (``at_top``: its answer card has them) and for the butler's
         own (its answer card is at the top level already)."""
        key = str(record.get("job_key") or "")
        if not key or record.get("at_top") or butler.is_butler_topic(record.get("topic_id"), record.get("chat_id")):
            return None, None
        from covey.app import answer_cards
        from covey.app.artifacts import answer_files
        try:
            if not answer_cards.holds_files(ctx.state_dir, key):
                return None, None
            job = find_job_record(ctx.state_dir, key)
            panel = answer_files(ctx.state_dir, job, str(record.get("topic_id") or ""), self.voice) if job else None
            ledger = answer_cards.ledger_of(ctx.state_dir, job, self.voice) if panel is not None else None
            return panel, (ledger or {}).get("status")
        except Exception as exc:  # noqa: BLE001 - the copy without them
            self.runtime.log(f"question top-level copy 产物 unavailable: {type(exc).__name__}")
            return None, None

    def listed_copy(self, ctx, record, drawn, *, check=True):
        """The copy of ``record`` drawn with its turn's 产物 at the upload's status ``drawn``, listed with that turn's
        answer cards (app/answer_cards.py register_copy). ``check``: just sent, so an upload that ended in between
        (its refresh did not know the copy yet) is painted now, once, not left at 上传中."""
        from covey.app import answer_cards
        try:
            answer_cards.register_copy(ctx.state_dir, record["job_key"], record["mirror_message_id"], record["token"],
                                       drawn)
            if check and self.copy_files(ctx, record)[1] != drawn:
                self.paint_mirror(ctx, record)
        except Exception as exc:  # noqa: BLE001 - the copy is out; its 产物 may lag
            self.runtime.log(f"question top-level copy 产物 not listed: {type(exc).__name__}")

    def repaint_copy(self, ctx, token):
        """Repaint the top-level copy of the question ``token`` from its record as it is now (app/artifacts.py,
        once the upload of its turn's 产物 ended or after a 重新上传); never raises."""
        path = ctx.state_dir / "questions" / (str(token) + ".json")
        try:
            with question_card_lock(path):
                if not path.exists():
                    return False
                return self.paint_mirror(ctx, json.loads(path.read_text()))
        except Exception as exc:  # noqa: BLE001 - the copy keeps its 重新上传
            self.runtime.log(f"question top-level copy repaint failed: {type(exc).__name__}")
            return False

    def show_answer_notice(self, ctx, chat, token, text, mid):
        """An answer that cannot be submitted yet is reported on one small card; when the answer
        goes through, that card says so instead of leaving a stale error behind."""
        path = ctx.state_dir / "questions" / (token + ".json")
        card = answer_notice_card(f"⚠️ **回答还没提交**：{text}。改好后在问题卡上再点“提交回答”。")
        source = json.loads(path.read_text()).get("answer_notice_message_id")
        if source and self.cards.repaint_browse_card(ctx, chat, mid, source, card):
            return

        def remember(message_id):
            update_question(path, lambda current: {**current, "answer_notice_message_id": message_id})
        if not self.messenger.publish_card(ctx, chat, mid, card, on_sent=remember, voice=self.voice):
            self.messenger.reply(ctx, chat, f"回答还没提交：{text}", self.voice.key("answer-incomplete", mid), voice=self.voice)

    @staticmethod
    def ask_block_for(ctx, record, log=None):
        """The "你问" block ({} without one) when ``record``'s standalone card goes to the chat's top level itself, a
        message of its own drawn as the top-level copy (``top_card``): the question of a quiet butler turn whose
        card has not gone out (core/placement.py Row.quiet, D1 2026-11-08), so asking opens no thread and needs no copy
        (``mirror``); None for any other question."""
        key = str(record.get("job_key") or "")
        job = find_job_record(ctx.state_dir, key) if key else None
        if not job or not placement.quiet(ctx.state_dir, job) or str(job.get("card_message_id") or "").startswith("om_"):
            return None
        from covey.app.answer_cards import asked
        try:
            return asked(ctx.state_dir, job, profile=ctx.profile, log=log) or {}
        except Exception:  # noqa: BLE001 - the card without it
            return {}

    # The standalone question card: its address survives task completion and restart.
    def publish(self, ctx, record, *, visible=None):
        top = None if record.get("at_top") else self.ask_block_for(ctx, record, self.runtime.log)
        moved = {}
        if top is not None:
            moved = {"at_top": True, "mirror_state": "at_top", **({"mirror_ask": top} if top else {})}
            record.update(moved)
            visible = {**visible, **moved} if visible else None
        if record.get("at_top"):
            ctx = dataclasses.replace(ctx, reply_to_message_id="")

        def remember(mid):
            mapping = ctx.state_dir / 'browse-cards' / (self.voice.safe_key(mid) + '.json')
            saved = json.loads(mapping.read_text())
            saved['question_token'] = record['token']
            write_private_text(mapping, json.dumps(saved))
            address = {'question_message_id': mid, 'card_id': saved['card_id'], **moved}
            record.update(address)
            # Persist the address before the publishing caller marks the form visible. Only the
            # address: an answer submitted while the card was being sent must not be written over.
            path = ctx.state_dir / 'questions' / (record['token'] + '.json')
            with resource_lock('question', path):
                if path.exists():
                    update_question(path, lambda current: {**current, **address})
                else:
                    write_private_text(path, json.dumps(record, ensure_ascii=False))

        return self.messenger.publish_card(ctx, record['chat_id'], 'question-' + record['token'],
                                                 standalone_card(ctx, visible or record), on_sent=remember, voice=self.voice)

    def paint_standalone(self, ctx, record):
        mid = record['question_message_id']
        path = ctx.state_dir / 'browse-cards' / (self.voice.safe_key(mid) + '.json')
        with resource_lock('card', record['card_id']):
            saved = json.loads(path.read_text())
            if (saved.get('card_id') != record['card_id'] or saved.get('topic_id') != ctx.topic_id
                    or saved.get('chat_id') != record['chat_id'] or saved.get('question_token') != record['token']):
                raise ValueError('问题卡片归属不一致')
            # Reserve even a failed update's sequence: its network outcome may be unknown.
            sequence = reserve(file_card(path, saved))
            result = self.messenger.put_card(ctx.profile, saved['card_id'], standalone_card(ctx, record), sequence=sequence,
                                             key=f"question-{record['token']}-{sequence}")
            if result is None:
                raise RuntimeError('问题卡片更新未确认')

    def update_continuation(self, ctx, job, status):
        if not job.message_id.startswith('answer-') or status not in {'running', 'done', 'failed', 'cancelled', 'waiting_user', 'interrupted'}:
            return
        token = job.message_id.removeprefix('answer-')
        path = ctx.state_dir / 'questions' / (token + '.json')

        def moved_on(record):
            if (record.get('topic_id') != ctx.topic_id or record.get('chat_id') != job.chat_id
                    or record.get('continuation_job_id') != job.job_id or record.get('delivery_state') == status):
                return None
            return {**record, 'delivery_state': status}
        with question_card_lock(path):
            with resource_lock('question', path):
                if not path.exists():
                    return
                record = update_question(path, moved_on, expected={'answered'})
            if record:
                self.questions.paint_question_state(ctx, record)
