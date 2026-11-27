"""The proposal card of 整理专项 and keeping it true: proposing a new project (``propose``), its topic select, sending it, and
redrawing or ending it as the joins go on (``follow``, docs/projects.md 整理专项). Below it: app/tidy_facts.py, app/tidy_words.py
(the rows' words), app/tidy_apply.py (a project made without a card)."""
from __future__ import annotations

import time
from pathlib import Path

from covey.app import project_loop
from covey.app import project_sorter as sorter
from covey.app import tidy_facts as facts
from covey.cards import project_proposal as cards
from covey.core import project_proposals as proposals
from covey.core import projects, quiet_hours
from covey.core.state import read_object, write_json
from covey.core.topics import label_title

CHOICES = 20  # Topics the proposal card's select offers: the proposal's, then the recent others.


class ProposalCards:
    """The proposal cards. ``runtime``: the log. ``messenger``, ``voice``: sending and redrawing. ``env``: its ``topics``, where the 辅助模型's
    calls go (app/assist.py). ``words``: app/tidy_words.py TopicWords, the rows of topics and of the 整理专项 card. ``applier``:
    app/tidy_apply.py Applier, which makes the project when it needs no card."""

    def __init__(self, runtime, messenger, voice, env, words, applier) -> None:
        self.runtime, self.messenger, self.voice, self.env = runtime, messenger, voice, env
        self.words, self.applier = words, applier

    def choices(self, ctx, chat, topics) -> list:
        """What the proposal card's topic select offers: ``topics`` ticked, then the recent others (CHOICES in all)."""
        state, now = Path(ctx.state_dir), time.time()
        others = [topic for topic in facts.recent_topics(state, chat, now) if topic not in topics and not sorter.hidden(state, topic)]
        found = []
        for topic in [*topics, *others][:CHOICES]:
            label, title = label_title(state, topic)
            found.append({"topic": topic, "label": label, "title": title, "chosen": topic in topics})
        return found

    def send_proposal(self, ctx, chat, record) -> str:
        state = Path(ctx.state_dir)
        topics = record.get("topics") or ()
        view = {**facts.proposal_view(state, record), "rows": self.words.topic_rows(ctx, chat, topics),
                "choices": self.choices(ctx, chat, topics)}
        sent = []
        text = f"这几个话题像是一件事：{cards.topics_line(view['labels'])}。建成专项「{view['name']}」？"
        if not self.messenger.send_message(ctx.profile, chat, text, self.voice.safe_key(self.voice.key("tidy-proposal", record["id"])),
                                           voice=self.voice, card=cards.proposal_card(view), on_card=sent.append):
            self.runtime.log(f"project tidy proposal not sent proposal={record['id'][:8]}")
            return ""
        message = sent[0] if sent else ""
        proposals.update_proposal(state, record["id"], lambda data: data.update(card={"message_id": message, "chat_id": chat}, view=view))
        if view["first"]:
            proposals.set_pref(state, first_sent=time.time())
        if view["ask_auto"]:
            proposals.set_pref(state, asked_auto=time.time())
        self.runtime.log(f"project tidy proposal sent proposal={record['id'][:8]} topics={len(record.get('topics') or ())}")
        return message or "text"

    # Proposals that follow the joins ------------------------------------------------------------------------------------

    def follow(self, ctx, chat) -> int:
        """The open proposals some of whose topics are now in a project, however they got there (建专项, 自动收, the lead's
        加话, a merge, by hand): a 新建 one keeps the rest when MIN_CREATE are left (its card redrawn with them), else it ends
        (已过时, the card says where they went); a 归进 one ends. Each tick; how many changed. Rules 1 and 3: each record is
        changed under its own lock, its card written after."""
        state, changed = Path(ctx.state_dir), 0
        for record in proposals.proposals(state, [proposals.OPEN]):
            kind, topics = record.get("kind"), list(dict.fromkeys(record.get("topics") or ()))
            if kind in facts.PROJECT_KINDS:
                try:
                    changed += self.follow_project(ctx, record)
                except Exception as exc:  # noqa: BLE001 - the next tick tries again
                    self.runtime.log(f"project tidy follow failed proposal={record['id'][:8]}: {type(exc).__name__}")
                continue
            if kind not in (proposals.CREATE, proposals.JOIN) or not topics:
                continue
            left = proposals.free(state, topics)
            if len(left) == len(topics):
                continue
            where = facts.went(state, [topic for topic in topics if topic not in left])
            try:
                if kind == proposals.CREATE and len(left) >= proposals.MIN_CREATE:
                    done = self.retopic(ctx, chat, record, left, f"{where}，这张卡只剩下面 {len(left)} 个话题。")
                else:
                    rest = (f"；剩下的 {facts.labels(state, left)} 一个话题成不了专项" if left else "") if kind == proposals.CREATE else ""
                    done = self.retire(ctx, record, f"{where}{rest}，这条不再提。")
            except Exception as exc:  # noqa: BLE001 - the next tick tries again
                self.runtime.log(f"project tidy follow failed proposal={record['id'][:8]}: {type(exc).__name__}")
                continue
            if done:
                changed += 1
                self.runtime.log(f"project tidy proposal followed proposal={record['id'][:8]} left={len(left)}")
        return changed + self.unrepeat(ctx)

    def follow_project(self, ctx, record) -> int:
        """An open proposal about a project (拆出, 合并, 移出, 改名, 收起) whose project ended, or whose topics left it, since:
        it ends as 已过时, or a split keeps the topics still there when
        MIN_CREATE are; its row on the 整理专项 card is redrawn. 1 when it changed."""
        state, kind, pid = Path(ctx.state_dir), record.get("kind"), str(record.get("project") or "")
        ids = list(record.get("projects") or ()) if kind == proposals.MERGE else [pid]
        ended = [other for other in ids if projects.read_record(state, other).get("state") not in (projects.ACTIVE, projects.PAUSED)]
        if ended:
            return int(self.retire(ctx, record, f"专项「{facts.project_name(state, ended[0])}」已经结束或并走了，这条不再提。"))
        if kind not in (proposals.SPLIT, proposals.LEAVE):
            return 0
        topics = list(dict.fromkeys(record.get("topics") or ()))
        kept, gone = facts.still_in(state, topics, pid)
        if len(kept) == len(topics):
            return 0
        if kind == proposals.LEAVE or len(kept) < proposals.MIN_CREATE:
            rest = "" if kind == proposals.LEAVE else "，剩下的不够另建一个专项"
            return int(self.retire(ctx, record, f"{gone}{rest}，这条不再提。"))
        done = []

        def edit(data):
            if data.get("state") == proposals.OPEN:
                data.update(topics=kept, note=f"{gone}，只拆剩下的 {len(kept)} 个")
                done.append(True)
        saved = proposals.update_proposal(state, record["id"], edit)
        if done:
            self.redraw_row(ctx, saved, line=self.words.line(state, saved, ctx), note=saved["note"], state=proposals.OPEN,
                            item=self.words.row_item(ctx, saved))
        return int(bool(done))

    def redraw_row(self, ctx, record, **row) -> bool:
        """The row of proposal ``record`` on the 整理专项 card it was listed on (its ``run``) changed by ``row`` and the card
        redrawn; False when it has no such row (a proposal with a card of its own)."""
        run = str(record.get("run") or "")
        if not run.replace("-", "").isalnum():
            return False
        path = facts.run_path(Path(ctx.state_dir), run)
        saved = read_object(path)
        rows = (saved.get("view") or {}).get("rows") or []
        found = [item for item in rows if item.get("proposal") == record["id"]]
        if not found or not saved.get("message_id"):
            return False
        for item in found:
            item.update({key: value for key, value in row.items() if value is not None})
        write_json(path, saved, ensure_ascii=False, indent=1)
        self.messenger.patch_or_tell(ctx.profile, saved["message_id"], cards.tidy_card(saved["view"]), voice=self.voice,
                                     what="「整理专项」卡")
        return True

    def unrepeat(self, ctx) -> int:
        """The cards of the same topics (or some of them) as a newer proposal still waiting, ended so only that one stays: the
        看板 already lists only it (core/project_proposals.py ``waiting``). How many."""
        state, gone = Path(ctx.state_dir), 0
        kept = proposals.waiting(state)
        for record in proposals.proposals(state, [proposals.OPEN]):
            mine = set(record.get("topics") or ())
            if (record.get("kind") != proposals.CREATE or not (record.get("card") or {}).get("message_id")
                    or any(record["id"] == other["id"] for other in kept)):
                continue
            other = next((other for other in kept if mine <= set(other["topics"]) or set(other["topics"]) <= mine), None)
            if other is not None and self.retire(ctx, record, f"和提议「{other.get('name') or ''}」是同一件事，留那一张，这条不再提。"):
                gone += 1
        return gone

    def retopic(self, ctx, chat, record, topics, changed) -> bool:
        """An open proposal now about ``topics``, its card redrawn with them and ``changed`` said on it; whether it was (False:
        decided meanwhile)."""
        state = Path(ctx.state_dir)
        topics = list(dict.fromkeys(topics))
        message = str((record.get("card") or {}).get("message_id") or "")
        view = {**(record.get("view") or facts.proposal_view(state, record)), "changed": changed,
                "labels": [label_title(state, topic) for topic in topics]}
        if message:  # A row of an older 整理专项 card has no card of its own to draw.
            view.update(rows=self.words.topic_rows(ctx, chat, topics), choices=self.choices(ctx, chat, topics))
        done = []

        def edit(data):
            if data.get("state") == proposals.OPEN:
                data.update(topics=topics, view=view)
                done.append(True)
        proposals.update_proposal(state, record["id"], edit)
        if done:
            self.patch_own(ctx, message, cards.proposal_card(view))
        return bool(done)

    def retire(self, ctx, record, note) -> bool:
        """An open proposal ended as 已过时 with ``note``, its card redrawn so; whether it was (False: decided meanwhile)."""
        state = Path(ctx.state_dir)
        view = {**(record.get("view") or facts.proposal_view(state, record)), "state": proposals.EXPIRED, "note": note,
                "lesson": False}
        done = []

        def edit(data):
            if data.get("state") == proposals.OPEN:
                data.update(state=proposals.EXPIRED, decided_at=time.time(), view=view)
                done.append(True)
        proposals.update_proposal(state, record["id"], edit)
        if done:
            own = str((record.get("card") or {}).get("message_id") or "")
            if own:
                self.patch_own(ctx, own, cards.proposal_card(view))
            else:  # A row on a 整理专项 card (拆出, 合并, 归进…): that row says it now.
                self.redraw_row(ctx, record, state=proposals.EXPIRED, note=note)
        return bool(done)

    def patch_own(self, ctx, message, card):
        """A proposal's own card redrawn; none when it has no card (a row of an older 整理专项 card, clicked on the 看板)."""
        return self.messenger.patch_card(ctx.profile, message, card) if message else False

    def propose(self, ctx, chat, made) -> str:
        """A new project the judge sees: made at once (``auto``, or the owner's 以后直接建), else a proposal card; '' when it
        is not proposed (以后别提议, turned down before, MAX_ACTIVE projects on and none could be folded away for it)."""
        state = Path(ctx.state_dir)
        prefs = proposals.prefs(state)
        # Topics already in a project are not proposed again.
        topics = proposals.free(state, made["topics"])
        if len(topics) < proposals.MIN_CREATE:
            return ""
        if topics != list(made["topics"]):
            made = {**made, "topics": topics, "id": proposals.proposal_id(proposals.CREATE, topics)}
        if not project_loop.room_possible(state):
            return ""
        if proposals.declined_like(state, proposals.CREATE, made["topics"]):
            return ""
        same = proposals.overlapping(state, topics)
        if same:
            # Mostly the topics of a proposal already waiting for him: no second card; its topics it lacks go onto that card.
            more = [topic for topic in topics if topic not in (same.get("topics") or ())]
            if not more:
                return ""
            note = f"又发现 {facts.labels(state, more)} 也是这件事，补进来了。"
            merged = proposals.free(state, [*same["topics"], *more])
            return "merged" if self.retopic(ctx, chat, same, merged, note) else ""
        if proposals.mode() == proposals.AUTO or prefs.get("auto_create"):
            saved, _ = proposals.put_proposal(state, {**made, "state": proposals.ACCEPTED, "decided_at": time.time()})
            if saved.get("state") != proposals.ACCEPTED:
                return ""
            pid = self.applier.create(ctx, chat, saved, owner_named=False)
            proposals.update_proposal(state, saved["id"], lambda data: data.update(
                state=proposals.DONE if pid else proposals.FAILED, project_made=pid))
            return "created" if pid else ""
        if prefs.get("no_proposals"):
            return ""
        saved, new = proposals.put_proposal(state, made)
        if not new or saved.get("state") != proposals.OPEN or (saved.get("card") or {}).get("message_id"):
            return ""
        self.gists(ctx, chat, saved.get("topics") or ())
        return "proposed" if self.send_proposal(ctx, chat, saved) else ""

    def release(self, ctx, chat, now) -> int:
        """The proposal cards an older release held for the morning (the real-time 整理's at night), sent once the quiet hours are over, each tried once as
        any proposal card is; how many went. Decided or merged into another meanwhile: as the record says now."""
        if quiet_hours.quiet(now):
            return 0
        state, sent = Path(ctx.state_dir), 0
        for record in proposals.proposals(state, (proposals.OPEN,)):
            if not record.get("held") or (record.get("card") or {}).get("message_id"):
                continue
            held = proposals.update_proposal(state, record["id"], lambda data: data.pop("held", None))
            if held.get("state") == proposals.OPEN and self.send_proposal(ctx, chat, held):
                sent += 1
        return sent

    def gists(self, ctx, chat, topics) -> None:
        """The 话题要点 of each of ``topics`` that has none, so its proposal card says what each is about (one read a topic;
        one that cannot be read keeps its title on the card)."""
        state, now = Path(ctx.state_dir), time.time()
        missing = [topic for topic in topics if not sorter.hidden(state, topic)
                   and sorter.needs_gist(state, topic, sorter.material(state, chat, topic, now).last_job)]
        if not missing:
            return
        from covey.app import assist
        where = sorter.Where(getattr(self.env, "topics", None), assist.BACKFILL)
        for topic in missing:  # Each read routed anew (app/assist.py): a model that went away is not asked again.
            if sorter.stops(sorter.backfill(state, chat, topic, where=where, log=self.runtime.log, now=now)):
                return
