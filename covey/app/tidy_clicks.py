"""The clicks on 整理专项's cards: the proposal card, a row of the 整理专项 card, the reason form, 移出 and 设置's 整理专项.
The owner's only, each click once (app/unified.py PANELS, docs/projects.md 整理专项). The top of the app/tidy_*.py modules below
app/tidy_web.py: it asks app/tidy_run.py to start a 整理, app/tidy_apply.py to do what was decided and app/tidy_proposals.py to
redraw the proposal's card."""
from __future__ import annotations

from pathlib import Path

from covey.app.card_lanes import after_ack
from covey.app import project_loop
from covey.app import tidy_facts as facts
from covey.cards import project_proposal as cards
from covey.core import project_proposals as proposals
from covey.core import projects
from covey.core.locks import resource_lock
from covey.core.state import claim_once, read_object, write_json
from covey.core.topics import label_title

BOARD_FROM = "board"  # A proposal's 建专项 / 暂不考虑 clicked on the 看板 (cards/board.py).
INBOUND = "inbound-messages"


class Clicks:
    """The clicks on the cards. ``runtime``: the owner's chat. ``messenger``, ``voice``: the words said back. ``shared``:
    app/tidy_state.py TidyState (``handed``, ``busy``). ``words``: app/tidy_words.py TopicWords. ``applier``: app/tidy_apply.py
    Applier. ``proposal_cards``: app/tidy_proposals.py ProposalCards. ``runs``: app/tidy_run.py Runs (``start``)."""

    def __init__(self, runtime, messenger, voice, shared, words, applier, proposal_cards, runs) -> None:
        self.runtime, self.messenger, self.voice, self.shared = runtime, messenger, voice, shared
        self.words, self.applier, self.proposal_cards, self.runs = words, applier, proposal_cards, runs

    @staticmethod
    def matches(raw):
        return isinstance(raw, dict) and raw.get("kind") in cards.KINDS

    def claim(self, ctx, event, raw, owner):
        """A click on one of 整理专项's cards (app/unified.py PANELS), else None. The owner's only; each click once."""
        if not self.matches(raw):
            return None
        if not owner:
            return "owner_only"
        event_id = str(event.get("event_id") or "")
        if not event_id or not claim_once(Path(ctx.state_dir) / INBOUND, "tidy-" + event_id):
            return "duplicate"
        kind, message = raw["kind"], str(event.get("message_id") or "")
        chat = str(event.get("chat_id") or "") or facts.owner_chat(self.runtime)
        if kind == cards.START_KIND:
            words = self.runs.start(ctx, origin="settings")
            after_ack(lambda: self.messenger.send_message(ctx.profile, chat, words, self.voice.safe_key(
                self.voice.key("tidy-start", event_id)), voice=self.voice))
            return "tidy_start"
        if kind == cards.OPEN_SETTINGS_KIND:
            return self.open_settings(ctx, chat, raw, event_id)
        if kind == cards.LEAVE_KIND:
            topic, pid = str(raw.get("topic_id") or ""), str(raw.get("project_id") or "")
            if not projects.TOPIC_RE.fullmatch(topic) or not projects.valid_id(pid):
                return "tidy_bad_value"

            def leave():
                words = self.applier.leave(ctx, chat, topic, pid)
                value = {"kind": cards.LESSON_KIND, "topic": topic, "project": pid}
                self.messenger.send_message(ctx.profile, chat, words, self.voice.safe_key(self.voice.key("tidy-leave", event_id)),
                                            reply_to_message_id=message, voice=self.voice, card=cards.lesson_card(words, value))
            after_ack(leave)
            return "tidy_leave"
        if kind == cards.LESSON_KIND:
            return self.lesson(ctx, event, raw, message)
        if raw.get("run"):
            return self.row_click(ctx, chat, event, raw, message)
        return self.proposal_click(ctx, chat, event, raw, message)

    def open_settings(self, ctx, chat, raw, event_id) -> str:
        """换辅助模型 or 看诊断 under a failed 整理: the 设置 card on 配置 with 辅助模型 open, or on 诊断 (app/settings_hub.py)."""
        from covey.cards.butler_panel import ASSIST_PART
        from covey.cards.settings_hub import CONFIG, DOCTOR
        hub = getattr(getattr(self.runs, "env", None), "settings", None)
        tab = DOCTOR if raw.get("tab") == "doctor" else CONFIG
        if hub is None:
            return "tidy_no_settings"
        after_ack(lambda: hub.open_on(ctx, chat, "tidy-settings-" + event_id, tab,
                                      section=ASSIST_PART if tab == CONFIG else ""))
        return "tidy_open_settings"

    def lesson(self, ctx, event, raw, message) -> str:
        from covey.app.card_actions import form_fields

        state = Path(ctx.state_dir)
        text = " ".join(str((form_fields(event) or {}).get(cards.WHY_FIELD) or "").split())
        if text:
            proposals.add_lesson(state, text, kind="leave" if raw.get("topic") else "decline")
        if raw.get("proposal"):
            record = proposals.read_proposal(state, str(raw["proposal"]))
            view = {**(record.get("view") or facts.proposal_view(state, record)), "lesson": False,
                    "note": str((record.get("view") or {}).get("note") or "") + ("（理由已记下）" if text else "")}
            proposals.update_proposal(state, record["id"], lambda data: data.update(view=view))
            after_ack(lambda: self.proposal_cards.patch_own(ctx, message, cards.proposal_card(view)))
        else:
            after_ack(lambda: self.proposal_cards.patch_own(ctx, message, cards.lesson_card(
                "理由已记下，整理专项以后照这个来。" if text else "好的，没记理由。")))
        return "tidy_lesson"

    def proposal_click(self, ctx, chat, event, raw, message) -> str:
        """A click on the proposal card, or on its block on the 看板 (``from`` BOARD_FROM): the 看板 then redrawn after
        whatever the click does (its follow-ups run in order in this lane)."""
        outcome = self.decide(ctx, chat, event, raw, message)
        hand = self.shared.handed.get(BOARD_FROM)
        if raw.get("from") == BOARD_FROM and hand is not None and message and outcome != "tidy_stale":
            after_ack(lambda: hand(ctx, message))
        return outcome

    def decide(self, ctx, chat, event, raw, message) -> str:
        """A click on the proposal card."""
        from covey.app.card_actions import form_fields

        state, kind = Path(ctx.state_dir), raw["kind"]
        record = proposals.read_proposal(state, str(raw.get("proposal") or ""))
        # 建专项 and 暂不考虑 on the 看板 (cards/board.py, ``from`` "board"): the
        # proposal's own card is the one redrawn, then the 看板 (``handed``).
        board = raw.get("from") == BOARD_FROM and kind in (cards.CREATE_KIND, cards.DECLINE_KIND)
        own = str((record or {}).get("card", {}).get("message_id") or "") if record else ""
        if not record or (not board and own not in ("", message)):
            return "tidy_stale"
        board_message, message = (message, own) if board else ("", message)
        view = dict(record.get("view") or facts.proposal_view(state, record))
        if kind == cards.AUTO_KIND:
            proposals.set_pref(state, auto_create=True)
            view["ask_auto"] = False
            proposals.update_proposal(state, record["id"], lambda data: data.update(view=view))
            after_ack(lambda: self.proposal_cards.patch_own(ctx, message, cards.proposal_card(view)))
            return "tidy_auto"
        if record.get("state") != proposals.OPEN:
            after_ack(lambda: self.proposal_cards.patch_own(ctx, message, cards.proposal_card({**view, "state": record["state"]})))
            return "tidy_decided"
        if kind == cards.RENAME_KIND:
            after_ack(lambda: self.proposal_cards.patch_own(ctx, message, cards.rename_form(view)))
            return "tidy_rename"
        if kind == cards.BACK_KIND:
            after_ack(lambda: self.proposal_cards.patch_own(ctx, message, cards.proposal_card(view)))
            return "tidy_back"
        if kind in (cards.DECLINE_KIND, cards.NEVER_KIND):
            never = kind == cards.NEVER_KIND
            try:
                proposals.update_proposal(state, record["id"], facts.claim(proposals.NEVER if never else proposals.DECLINED))
            except facts.Taken:
                return "tidy_decided"
            proposals.set_pref(state, accepted_in_row=0, **({"no_proposals": True} if never else {}))
            view.update(state=proposals.NEVER if never else proposals.DECLINED, lesson=True,
                        note="以后不再提议建专项；要开专项，跟管家说“开个专项”。" if never else
                        "不管了：这几个话题不会再这样提，等又多出相关的话题才会再提。")
            proposals.update_proposal(state, record["id"], lambda data: data.update(view=view))
            after_ack(lambda: self.proposal_cards.patch_own(ctx, message, cards.proposal_card(view)))
            return "tidy_never" if never else "tidy_decline"
        # 建专项, or the rename form's 建专项.
        fields = form_fields(event) or {}
        name = proposals.clean_name(fields.get(cards.NAME_FIELD)) if kind == cards.NAMED_KIND else record.get("name")
        goal = (" ".join(str(fields.get(cards.GOAL_FIELD) or "").split())[:200] if cards.GOAL_FIELD in fields
                else record.get("goal"))
        if kind == cards.NAMED_KIND and not name:
            return "tidy_no_name"
        named = name != record.get("name")  # The owner's own name: 整理专项 never renames it, and 以后直接建 is not offered for it.
        topics, unknown = facts.chosen(state, chat, view, fields)
        if topics is None:
            topics = list(record.get("topics") or ())
        # A topic already in a project stays there (app/projects.py step_members): said, not listed as the new one's.
        taken = [topic for topic in topics if projects.topic_project(state, topic) is not None]
        topics = [topic for topic in topics if topic not in taken]
        elsewhere = facts.elsewhere(state, taken)
        if not topics and taken:
            after_ack(lambda: self.messenger.send_message(ctx.profile, chat, f"专项没建：{elsewhere}，一个话题只能在一个专项里。",
                                                          self.voice.safe_key(self.voice.key("tidy-taken", str(event.get("event_id") or ""))),
                                                          voice=self.voice))
            return "tidy_no_topics"
        if not topics:
            after_ack(lambda: self.messenger.send_message(ctx.profile, chat, "没选话题，专项没建：在卡上勾上话题或填 T 号再点「建专项」。",
                                                          self.voice.safe_key(self.voice.key("tidy-none", str(event.get("event_id") or ""))),
                                                          voice=self.voice))
            return "tidy_no_topics"
        record = {**record, "topics": topics}
        taking = facts.claim(proposals.ACCEPTED)

        def accept(data):
            taking(data)
            data.update(name=name, goal=goal or "", topics=topics)
        try:
            proposals.update_proposal(state, record["id"], accept)
        except facts.Taken:
            return "tidy_decided"

        def build():
            room = project_loop.make_room(state, self.runtime.log)
            if not room.ok:
                pid, words = "", "没建：" + room.words + "。之后再说“整理一下专项”。"
            else:
                pid = self.applier.create(ctx, chat, {**record, "name": name, "goal": goal}, owner_named=named, name=name, goal=goal)
                words = (f"已建专项「{name}」：专项卡在聊天区，负责人接着跟进。" + project_loop.room_words(room) if pid
                         else "专项没建成，过一会儿再说“整理一下专项”。")
                if pid and unknown:
                    words += f"（{'、'.join(unknown)} 不是你最近的话题，没加）"
                if pid and taken:
                    words += f"（{elsewhere}，没加）"
            proposals.update_proposal(state, record["id"], lambda data: data.update(
                state=proposals.DONE if pid else proposals.FAILED, project_made=pid))

            def count(data):
                prefs = data.get("prefs") or {}
                data["prefs"] = {**prefs, "accepted_in_row": 0 if named or not pid else int(prefs.get("accepted_in_row") or 0) + 1}
            proposals.change(state, count)
            rows = self.words.topic_rows(ctx, chat, topics) if view.get("rows") else view.get("rows")
            shown = {**view, "name": name, "goal": goal, "state": proposals.DONE if pid else proposals.FAILED, "note": words,
                     "ask_auto": False, "rows": rows, "labels": [label_title(state, topic) for topic in topics]}
            proposals.update_proposal(state, record["id"], lambda data: data.update(view=shown))
            self.proposal_cards.patch_own(ctx, message, cards.proposal_card(shown))
        after_ack(build)
        return "tidy_named" if named else "tidy_create"

    def row_click(self, ctx, chat, event, raw, message) -> str:
        """好, 不用 and 全部照办 on the 整理专项 card."""
        state, run = Path(ctx.state_dir), str(raw.get("run") or "")
        if not run.replace("-", "").isalnum():
            return "tidy_bad_value"
        path = facts.run_path(state, run)
        record = read_object(path)
        if not record or record.get("message_id") != message:
            return "tidy_stale"
        if self.shared.busy.locked() and (record.get("view") or {}).get("state") == "running":
            return "tidy_running"
        rows = [row for row in (record.get("view") or {}).get("rows") or ()]
        chosen = [row for row in rows if row.get("state") == proposals.OPEN and (raw["kind"] == cards.ALL_KIND
                                                                              or row.get("proposal") == raw.get("proposal"))]
        if not chosen:
            return "tidy_decided"

        def act():
            done = {}  # proposal id -> what its row says now
            for row in chosen:
                pid, decline = row["proposal"], raw["kind"] == cards.DECLINE_KIND
                try:  # Taken only while still open, under its record's lock: the card and the web page may click it at once.
                    proposal = proposals.update_proposal(state, pid, facts.claim(proposals.DECLINED if decline else proposals.ACCEPTED))
                except facts.Taken as taken:
                    done[pid] = {"state": taken.state or proposals.EXPIRED}
                    continue
                except KeyError:
                    done[pid] = {"state": proposals.EXPIRED}
                    continue
                if decline:
                    done[pid] = {"state": proposals.DECLINED, "note": "不用"}
                    continue
                try:
                    ok, words = self.applier.apply(ctx, chat, proposal)
                except Exception as exc:  # noqa: BLE001 - this row only
                    ok, words = False, f"出错了（{type(exc).__name__}）"
                proposals.update_proposal(state, pid, lambda data: data.update(
                    state=proposals.DONE if ok else proposals.FAILED, note=words))
                done[pid] = {"state": proposals.DONE if ok else proposals.FAILED, "note": words}
            with resource_lock("record", str(path)):  # Only these rows: another click may have decided another one meanwhile.
                saved = read_object(path)
                view = dict(saved.get("view") or {})
                view["rows"] = [{**row, **done.get(row.get("proposal"), {})} for row in view.get("rows") or ()]
                saved["view"] = view
                write_json(path, saved, ensure_ascii=False, indent=1)
            if message:  # A 整理 whose card was never sent has nothing to redraw.
                self.messenger.patch_or_tell(ctx.profile, message, cards.tidy_card(view), voice=self.voice, chat_id=chat,
                                             what="「整理专项」卡")
        after_ack(act)
        return {cards.OK_KIND: "tidy_ok", cards.ALL_KIND: "tidy_all"}.get(raw["kind"], "tidy_row_decline")
