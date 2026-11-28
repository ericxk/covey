"""Where a question is shown: its element on the task card, its standalone card (whose address
survives task completion and restart), the notice for an answer not submitted yet, and the
continuation's state on the card. ``question_card_lock`` orders one question's publishing and repainting.

A 话题区's question (a topic the butler opened, with COVEY_BUTLER=1; a project's topic too, once the question is the
owner's: one held for the lead is copied when the lead hands it on, app/triage.py show) is also shown at the private
chat's top level (``mirror``): the same form, quoting the butler's card, with
what the turn wrote before it asked above it; one longer than MIRROR_OUTPUT_BYTES, or that would take the copy past
a card's room (cards/pages.py fits), goes on cards of its own first, each with the copy's row (``output_apart``).
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

from covey.app.card_lanes import clicked_message
from covey.core.card_sequence import file_card, reserve
from covey.core.locks import resource_lock
from covey.cards.pages import fits
from covey.cards.question_cards import (OTHERS, SET_ASIDE, answer_notice_card, mirror_fit, output_pages, question_card,
                                        question_element, triage_element)
from covey.core import butler, memory_words, placement, triage
from covey.core.jobs import find_job_record
from covey.core.state import read_object, write_private_text
from covey.core.store import update_question
from covey.core.topics import route_message, topic_label, topic_title
from covey.feishu.messenger import CardAbout

MIRROR_OUTPUT_BYTES = 12000  # A longer output goes on cards of its own before the top-level question card.


def owner_view(ctx, record):
    """``record`` as its cards draw it (core/memory_words.py question_view): in the owner's private chat the memory short
    codes in its words are their entries' titles; the ids and the options' labels, which an answer is matched by, as they
    are. The record itself is never changed."""
    return memory_words.question_view(record, ctx.state_dir)


def standalone_card(ctx, record):
    """The standalone card of ``record``, its topic named on it; drawn as the top-level copy when it
    is at the top level itself (``at_top``)."""
    record = owner_view(ctx, record)
    if record.get("at_top"):
        return top_card(ctx, record)
    # In its topic's thread it opens with the topic's row, as the topic's other cards do (docs/ui.md 5.2 问题卡).
    return question_card(record, topic_label(ctx.state_dir, record["topic_id"]),
                         topic_title(ctx.state_dir, record["topic_id"]))


def top_card(ctx, record, files=None):
    """The top-level copy of ``record``'s card (``mirror``), its "你问" block as it was sent; ``files``: the asking
    turn's 产物 panel (QuestionDisplay.copy_files)."""
    return top_fit(ctx, record, files).card


def top_fit(ctx, record, files=None):
    """``top_card`` as cards/question_cards.py mirror_fit draws it: the card, and whether it holds all of it."""
    record = owner_view(ctx, record)
    return mirror_fit(record, topic_label(ctx.state_dir, record["topic_id"]), record.get("mirror_output") or "",
                      ask=record.get("mirror_ask"), files=files)


QUESTION_CARD, QUESTION_COPY = "问题卡", "问题卡副本"
WEB_ANSWER = "，或到网页工作台答题"


def answer_where(ctx, record, *, copy=False, link=None) -> str:
    """Where the owner answers ``record``'s questions when a card of them could not be shown (feishu/messenger.py
    CardAbout ``where``): the card in the topic for its top-level copy (``copy``; ``link``, text -> text, makes its T
    number a link), else a reply in the topic, or at the top level for a question there itself; the web page too, but
    not for the butler's own questions (docs/web.md 答题: answered in Feishu only)."""
    web = "" if butler.is_butler_topic(record.get("topic_id"), record.get("chat_id")) else WEB_ANSWER
    if copy:
        words = f"到 {topic_label(ctx.state_dir, record.get('topic_id') or '') or '话题'} 里的问题卡作答{web}"
        return link(words) if link else words
    return ("直接回复作答" if record.get("at_top") else "可以直接在话题里回复作答") + web


def owner_started(state_dir, job) -> bool:
    """Whether the turn ``job`` of a project's topic is the owner's own, not work its lead handed it: started by his message
    in the topic ("om_…") or by his answer to its question ("answer-<token>", one the lead did not answer for him,
    app/triage.py), an automatic retry followed back to where its chain began (app/relay_targets.py origin_message). A
    turn the lead handed on (project_topic_start, project_topic_send: "main-…"), a wake-up or anything else the Bridge
    made up is not. Never raises."""
    from covey.app.relay_targets import origin_message

    try:
        message = origin_message(state_dir, job)
    except Exception:  # noqa: BLE001 - as the lead's: nothing of the owner's is quoted
        return False
    if message.startswith("om_"):
        return True
    token = message.removeprefix("answer-") if message.startswith("answer-") else ""
    if not token or "/" in token or token.startswith("."):
        return False
    question = read_object(Path(state_dir) / "questions" / (token + ".json"))
    return bool(question) and triage.state(question) != triage.ANSWERED


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

    def with_others(self, ctx, record):
        """``record`` with what else waits for the owner (``OTHERS``, never saved) once he has settled it himself (an
        answer, a 补充说明 alone, a message of the topic taken as one, 不处理; a feedback or a cancel is not one): the 看板's
        count (app/overview.py waiting_count), in the chat that has a 看板 card (the private chat, a personal group)
        only; the record as it was when nothing else waits or the count cannot be read."""
        if record.get("status") not in ("answered", SET_ASIDE) or record.get("response_kind") == "feedback":
            return record
        chat, access = record.get("chat_id") or "", self.runtime.access
        if not chat or chat != access.notify_chat() and chat not in getattr(access, "agent_groups", ()):
            return record
        try:
            from covey.app.overview import waiting_count  # Not at start: a task runner never needs 手上的事 until it paints.

            # The private chat's 等你 has 整理专项's proposals too, as its 看板 counts them; a group's has none.
            waiting = waiting_count(ctx.state_dir, chat, time.time(), proposals=chat not in getattr(access, "agent_groups", ()))
        except Exception as exc:  # noqa: BLE001 - the card without its line
            self.runtime.log(f"waiting count unreadable: {type(exc).__name__}")
            return record
        return {**record, OTHERS: {"n": waiting, "board": True}} if waiting else record

    def paint(self, ctx, record):
        record = self.with_others(ctx, record)
        if record.get("display_pending") or (record.get("body_delivery_required")
                and not self.questions.checkpoints.result_delivered(ctx, record)):
            return
        # The card clicked first (an answer on the top-level copy): the owner sees it change a CardKit write (~0.3 s) sooner.
        mirror_first = bool(record.get("mirror_message_id")) and clicked_message() == record["mirror_message_id"]
        try:
            if mirror_first:
                self.paint_mirror(ctx, record)
            if record.get("question_message_id"):
                self.questions.paint_standalone_question(ctx, record)
                if not mirror_first:
                    self.paint_mirror(ctx, record)
                return
            with self.runtime.jobs.lock:
                job = self.runtime.jobs.by_id.get(int(record["job_id"]))
                if job and (job.topic_id != ctx.topic_id or job.chat_id != record["chat_id"]):
                    job = None
            if job:
                self.cards.set_task_element(ctx, job, question_element(owner_view(ctx, record)))
            elif record.get("card_id"):
                original = self.harness.topic_record(ctx, record["chat_id"], record["job_id"])
                if original:
                    self.cards.update_record_panel(ctx, record["chat_id"], "answer-state-" + str(time.time_ns()),
                        {"message_id": original.get("card_message_id", "")}, original, question_element(owner_view(ctx, record)))
            if not mirror_first:
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
        drawn = owner_view(ctx, record)
        element = triage_element(drawn) if triage.state(record) == triage.ESCALATED and record.get("status") == "pending" else question_element(drawn)
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

    def member_speaker(self, ctx, record, job=None):
        """The speaker line atop a project topic's question copy at the top level for work its lead handed it: the topic
        asks (cards/answer_card.py ask_block, ``doing`` member) and quotes nothing, the words would be the lead's. The
        line opens the topic's thread at the question (its card, else the asking turn's card, app/answer_cards.py
        ask_url), as an ordinary topic's copy does; None when the topic cannot be named."""
        from covey.app.answer_cards import ask_url, avatar, speaker
        from covey.cards.answer_card import ask_block

        topic, chat = str(record.get("topic_id") or ""), str(record.get("chat_id") or "")
        who = speaker(ctx.state_dir, topic, chat, self.runtime.log)
        if not who:
            return None
        picture = avatar(ctx.state_dir, topic, chat, getattr(ctx, "profile", None), self.runtime.log)
        turn = job if job is not None else {"chat_id": chat, "topic_id": topic, "job_id": record.get("job_id")}
        try:
            url = ask_url(ctx.state_dir, turn, topic_id=topic, at=[m for m in (str(record.get("question_message_id") or ""),) if m],
                          profile=getattr(ctx, "profile", None), log=self.runtime.log)
        except Exception as exc:  # noqa: BLE001 - the line without its link, said
            self.runtime.log(f"question copy link failed token={str(record.get('token') or '')[:10]}: {type(exc).__name__}")
            url = ""
        return ask_block("", who=who, doing="member", avatar=picture, url=url)

    def member_ask(self, ctx, record, job=None):
        """The row atop a project topic's question copy (T658: it could not be clicked, and a turn the owner started there
        lost his words): a turn of the owner's own in that topic (``owner_started``: his message there or his answer to its
        question) quotes him as an ordinary topic's copy does (relay_targets.ask_for: 你问 / 你答, the line that opens, the
        link into the thread at the question); a turn the lead handed it says so (``member_speaker``)."""
        from covey.app.relay_targets import ask_for

        topic = str(record.get("topic_id") or "")
        if job is not None and owner_started(ctx.state_dir, job):
            ask = ask_for(ctx.state_dir, job, {"kind": "follow", "topic_id": topic}, ctx.profile,
                          at=str(record.get("question_message_id") or ""), log=self.runtime.log, doing="waiting")
            if ask:
                return ask
        return self.member_speaker(ctx, record, job)

    # The top-level copy of a 话题区's question.
    def mirror(self, ctx, record, output="", job=None, *, quiet=False):
        """Show the pending question ``record`` at the top level too, once, when its topic is a 话题区, or the butler's
        , whose card is in the owner's message's thread (core/placement.py question_copy); its message id,
        '' when it is not shown there (another topic, no butler request it came from, already tried). Threaded,
        it is a message of its own with a "你问" block (``job``: the turn that asks), quoting no card. Each try is one
        log line ``main relay state=… outcome=question``, as a relay's (app/relay_send.py send_once). A copy that could
        not be shown gets one line in its place, saying why and where to answer (``answer_where``), unless ``quiet``: the
        caller's 等你 relay follows and says that (app/butler_relay.py after_turn)."""
        from covey.app.relay_targets import area_target, ask_for

        chat, token = record.get("chat_id") or "", record.get("token") or ""
        place = placement.plan(ctx.state_dir, {"topic_id": record.get("topic_id"), "chat_id": chat})
        if (not place.row.question_copy or record.get("status") != "pending" or record.get("display_pending")):
            return ""
        target = (area_target(ctx.state_dir, record["topic_id"], chat) if place.traits.relays
                  else {"chat_id": chat, "reply_to": "", "label": "", "kind": "main"})
        if not target and place.role == placement.MEMBER_ROLE:
            # A project's topic was opened by its lead, never by a request of the butler, so it has no 话题区 target:
            # its question goes to the top level all the same (core/placement.py: only a question that reaches the
            # owner is copied there).
            target = {"chat_id": chat, "reply_to": "", "label": "", "kind": "member", "topic_id": record["topic_id"]}
        if not target:
            return ""
        # A member's turn the lead asked for quotes no words of the owner's (they would be the lead's), only who asks,
        # "T46 标题 办负责人派的事，要你定" (cards/answer_card.py says, docs/ui.md 5.3); one the owner started in the topic
        # quotes him (``member_ask``). Either row opens the topic's thread.
        if target["kind"] == "member":
            ask = self.member_ask(ctx, record, job)
        else:
            ask = (ask_for(ctx.state_dir, job, self._asked_target(ctx, job, target) if place.traits.relays else target,
                           ctx.profile, at=str(record.get("question_message_id") or ""), log=self.runtime.log,
                           doing="waiting")
                   if place.row.ask_block and job is not None else None)
        reply_to = "" if place.row.question_copy == placement.TOP else target["reply_to"]
        path = ctx.state_dir / "questions" / (token + ".json")
        text = str(output or "").strip()
        apart = len(text.encode("utf-8")) > MIRROR_OUTPUT_BYTES
        with question_card_lock(path):
            files, drawn = self.copy_files(ctx, record)
            if text and not apart:
                # The copy with the output on it past a card's room (the owner's words, many long questions, samples,
                # 产物): the output goes apart first, before a sample is left out (mirror_fit ``whole``), so the copy
                # itself is never refused and shows what it can.
                fit = top_fit(ctx, {**record, "mirror_output": text, **({"mirror_ask": ask} if ask else {})}, files)
                apart = not (fit.whole and fits(fit.card, cardkit=True))
            current = update_question(path, lambda r: None if r.get("mirror_state") else
                                      {**r, "mirror_state": "creating", "mirror_output": "" if apart else text,
                                       **({"mirror_ask": ask} if ask else {})})
            if current is None:
                return ""
            top = dataclasses.replace(ctx, reply_to_message_id=reply_to)
            if apart:
                self.output_apart(ctx, current, text, ask, reply_to)
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

            about = CardAbout(QUESTION_COPY, quiet=quiet, where=lambda: answer_where(
                ctx, record, copy=True, link=lambda words: self.linked(ctx, chat, words)))
            try:
                self.messenger.publish_card(top, chat, "question-top-" + token, top_card(ctx, current, files),
                                            on_sent=remember, voice=self.voice, about=about)
            except Exception as exc:  # noqa: BLE001 - as unconfirmed: never sent again
                self.runtime.log(f"question top-level copy failed token={token[:10]}: {type(exc).__name__}")
            latest = update_question(path, lambda r: {**r, "mirror_state": "sent" if sent else "uncertain"})
            if sent and files is not None:
                self.listed_copy(ctx, latest or current, drawn)
        self.runtime.log(f"main relay state={'sent' if sent else 'uncertain'} outcome=question job=#{record.get('job_id')}"
                         f" kind={target.get('kind')} token={token[:10]} message={(sent[0] if sent else '')[-8:]}")
        return sent[0] if sent else ""

    @staticmethod
    def linked(ctx, chat, words):
        """``words`` with their T numbers links to their topics (app/topic_links.py linkify); never raises."""
        from covey.app.topic_links import linkify

        return linkify(words, ctx.state_dir, chat, profile=ctx.profile)

    def output_apart(self, ctx, record, text, ask, reply_to):
        """What the turn wrote before it asked (``text``), above the top-level copy of the question ``record`` when the
        copy cannot hold it too (``mirror``): on cards of its own sent before it (cards/question_cards.py output_pages:
        the copy's row on each, "1/2" in it, the owner's words that open under the first), its T numbers links
        (app/relay_send.py linked; the topic's own plain, the row opens it), each card the topic's message, a quote of it
        going to the topic as a quote of the copy does (app/relay_send.py remember_sent). Sent once, with the copy (its
        mirror_state); never raises: the copy goes all the same."""
        from covey.app.relay_send import linked, remember_sent
        from covey.feishu.message_request import optimize_markdown

        chat, token, topic = str(record.get("chat_id") or ""), str(record.get("token") or ""), str(record.get("topic_id") or "")
        skip, sent = (topic,), []
        try:
            text = memory_words.for_chat(text, ctx.state_dir, chat)  # Display only, as Markdown before it is a card.
            cut = output_pages(optimize_markdown(text, step_headings=False), topic_label(ctx.state_dir, topic), ask,
                               link=lambda words: linked(ctx.state_dir, chat, None, words, ctx.profile, skip=skip)[1],
                               finish=lambda built: linked(ctx.state_dir, chat, built, "", ctx.profile, skip=skip)[0])
            for n, page in enumerate(cut.pages, 1):
                key = self.voice.key("question-top-output", token if n == 1 else f"{token}-p{n}")
                if not self.messenger.send_message(ctx.profile, chat, page.text, key, reply_to_message_id=reply_to,
                                                   voice=self.voice, card=page.card, on_sent=sent.append):
                    self.runtime.log(f"question top-level output not confirmed token={token[:10]} card={n}/{len(cut.pages)}")
                    break
        except Exception as exc:  # noqa: BLE001 - the copy goes all the same
            self.runtime.log(f"question top-level output failed token={token[:10]}: {type(exc).__name__}")
        remember_sent(ctx.state_dir, chat, topic, sent, skip_errors=True)

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
        if not self.messenger.publish_card(ctx, chat, mid, card, on_sent=remember, voice=self.voice,
                                           about=CardAbout("回答提示", quiet=True)):  # The line below says it.
            self.messenger.reply(ctx, chat, f"回答还没提交：{text}", self.voice.key("answer-incomplete", mid), voice=self.voice)

    @staticmethod
    def ask_block_for(ctx, record, log=None):
        """The "你问" block ({} without one) when ``record``'s standalone card goes to the chat's top level itself, a
        message of its own drawn as the top-level copy (``top_card``): the question of a quiet butler turn whose
        card has not gone out (core/placement.py Row.quiet), so asking opens no thread and needs no copy
        (``mirror``); None for any other question."""
        key = str(record.get("job_key") or "")
        job = find_job_record(ctx.state_dir, key) if key else None
        if not job or not placement.quiet(ctx.state_dir, job) or str(job.get("card_message_id") or "").startswith("om_"):
            return None
        from covey.app.answer_cards import asked
        try:
            return asked(ctx.state_dir, job, profile=ctx.profile, log=log, doing="waiting") or {}
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
                                           standalone_card(ctx, visible or record), on_sent=remember, voice=self.voice,
                                           about=CardAbout(QUESTION_CARD, where=answer_where(ctx, record)))

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
