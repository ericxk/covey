"""The proposal card of 整理专项 and keeping it true: its rows, its topic select, its view, sending it, and redrawing or ending it
as the joins go on (``follow``, docs/butler.md 整理专项)."""
from __future__ import annotations

import re
import time
from pathlib import Path

from covey.app import project_sorter as sorter
from covey.app import topic_links
from covey.app.tidy_facts import PROJECT_KINDS, labels, latest, project_name, where_now
from covey.cards import project_proposal as cards
from covey.core import project_proposals as proposals
from covey.core import projects, topic_gist
from covey.core.butler import is_butler_topic
from covey.core.state import read_object, write_json
from covey.core.topics import label_title

CHOICES = 20  # Topics the proposal card's select offers: the proposal's, then the recent others.


class ProposalCards:
    """``ProjectTidy``'s proposal cards. Uses ``runtime``, ``messenger``, ``voice``, ``link`` of app/project_tidy.py ProjectTidy."""

    def topic_rows(self, ctx, chat, topics) -> list:
        """The proposal card's rows (cards/project_proposal.py topic_lines): the T number and its link, the title, the
        本质 of its 话题要点, the days it has gone on and the owner's turns. The links read Feishu: outside every lock."""
        state, now, rows = Path(ctx.state_dir), time.time(), []
        for topic in topics:
            label, title = label_title(state, topic)
            found = sorter.material(state, chat, topic, now)
            try:
                link = self.link(state, topic, profile=getattr(ctx, "profile", None)) if label else ""
            except Exception:  # noqa: BLE001 - a T number without its link, as before
                link = ""
            essence = str(topic_gist.read(state, topic).get("essence") or found.gist.get("essence") or "")
            rows.append({"label": label, "title": title, "link": link, "essence": essence,
                         "now": where_now(state, chat, topic) or latest(found.conclusions),
                         "days": round(found.days, 1), "turns": found.turns})
        return rows

    def choices(self, ctx, chat, topics) -> list:
        """What the proposal card's topic select offers: ``topics`` ticked, then the recent others (CHOICES in all)."""
        state, now = Path(ctx.state_dir), time.time()
        others = [topic for topic in self.recent_topics(state, chat, now) if topic not in topics and not sorter.hidden(state, topic)]
        found = []
        for topic in [*topics, *others][:CHOICES]:
            label, title = label_title(state, topic)
            found.append({"topic": topic, "label": label, "title": title, "chosen": topic in topics})
        return found

    @staticmethod
    def chosen(state, chat, view, fields) -> tuple:
        """(the topics the owner left ticked or added on the proposal card, the T numbers he typed that are no topic of his).
        A card without choices (sent before them) keeps the proposal's topics."""
        choices = view.get("choices") or []
        if not choices or (cards.TOPICS_FIELD not in fields and cards.MORE_FIELD not in fields):  # 建专项 or the rename form
            return None, []
        picked = fields.get(cards.TOPICS_FIELD)
        picked = picked if isinstance(picked, list) else ([picked] if isinstance(picked, str) and picked else [])
        topics = [choices[int(value)]["topic"] for value in picked
                  if str(value).isdigit() and int(value) < len(choices)]
        numbers, unknown = topic_links.topic_numbers(state, chat), []
        for word in re.findall(r"[Tt]?\d+", str(fields.get(cards.MORE_FIELD) or "")):
            topic = numbers.get(int(word.lstrip("Tt")))
            if topic and not sorter.hidden(state, topic) and not is_butler_topic(topic, chat):
                topics.append(topic)
            else:
                unknown.append("T" + word.lstrip("Tt"))
        return list(dict.fromkeys(topics)), unknown

    def proposal_view(self, state, record) -> dict:
        prefs = proposals.prefs(state)
        accepted = int(prefs.get("accepted_in_row") or 0)
        return {"proposal": record["id"], "labels": [label_title(state, topic) for topic in record.get("topics") or ()],
                "name": record.get("name"), "goal": record.get("goal"), "why": record.get("why"),
                "first": not prefs.get("first_sent"), "accepted": accepted,
                "ask_auto": accepted >= proposals.ACCEPTS_TO_ASK and not prefs.get("auto_create") and not prefs.get("asked_auto"),
                "state": record.get("state") or proposals.OPEN}

    def send_proposal(self, ctx, chat, record) -> str:
        state = Path(ctx.state_dir)
        topics = record.get("topics") or ()
        view = {**self.proposal_view(state, record), "rows": self.topic_rows(ctx, chat, topics),
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
            if kind in PROJECT_KINDS:
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
            went = self.went(state, [topic for topic in topics if topic not in left])
            try:
                if kind == proposals.CREATE and len(left) >= proposals.MIN_CREATE:
                    done = self.retopic(ctx, chat, record, left, f"{went}，这张卡只剩下面 {len(left)} 个话题。")
                else:
                    rest = (f"；剩下的 {labels(state, left)} 一个话题成不了专项" if left else "") if kind == proposals.CREATE else ""
                    done = self.retire(ctx, record, f"{went}{rest}，这条不再提。")
            except Exception as exc:  # noqa: BLE001 - the next tick tries again
                self.runtime.log(f"project tidy follow failed proposal={record['id'][:8]}: {type(exc).__name__}")
                continue
            if done:
                changed += 1
                self.runtime.log(f"project tidy proposal followed proposal={record['id'][:8]} left={len(left)}")
        return changed + self.unrepeat(ctx)

    def follow_project(self, ctx, record) -> int:
        """An open proposal about a project (拆出, 合并, 移出, 改名, 收起) whose project ended, or whose topics left it, since
        (T574, the owner, 2026-11-20: 等着的拆分提议跟着话题变): it ends as 已过时, or a split keeps the topics still there when
        MIN_CREATE are; its row on the 整理专项 card is redrawn. 1 when it changed."""
        state, kind, pid = Path(ctx.state_dir), record.get("kind"), str(record.get("project") or "")
        ids = list(record.get("projects") or ()) if kind == proposals.MERGE else [pid]
        ended = [other for other in ids if projects.read_record(state, other).get("state") not in (projects.ACTIVE, projects.PAUSED)]
        if ended:
            return int(self.retire(ctx, record, f"专项「{project_name(state, ended[0])}」已经结束或并走了，这条不再提。"))
        if kind not in (proposals.SPLIT, proposals.LEAVE):
            return 0
        topics = list(dict.fromkeys(record.get("topics") or ()))
        kept, gone = self.still_in(state, topics, pid)
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
            self.redraw_row(ctx, saved, line=self.line(state, saved, ctx), note=saved["note"], state=proposals.OPEN,
                            item=self.row_item(ctx, saved))
        return int(bool(done))

    def redraw_row(self, ctx, record, **row) -> bool:
        """The row of proposal ``record`` on the 整理专项 card it was listed on (its ``run``) changed by ``row`` and the card
        redrawn; False when it has no such row (a proposal with a card of its own)."""
        run = str(record.get("run") or "")
        if not run.replace("-", "").isalnum():
            return False
        path = self.run_path(Path(ctx.state_dir), run)
        saved = read_object(path)
        rows = (saved.get("view") or {}).get("rows") or []
        found = [item for item in rows if item.get("proposal") == record["id"]]
        if not found or not saved.get("message_id"):
            return False
        for item in found:
            item.update({key: value for key, value in row.items() if value is not None})
        write_json(path, saved, ensure_ascii=False, indent=1)
        self.messenger.patch_card(ctx.profile, saved["message_id"], cards.tidy_card(saved["view"]))
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

    def went(self, state, topics) -> str:
        """Where ``topics`` went: T519、T531 已归进专项「X」 (one clause a project)."""
        by = {}
        for topic in topics:
            found = projects.topic_project(state, topic)
            by.setdefault(found.id if found is not None else "", []).append(topic)
        return "；".join(f"{labels(state, group)} 已归进专项「{project_name(state, pid)}」" if pid
                        else f"{labels(state, group)} 已归进专项" for pid, group in by.items())

    def retopic(self, ctx, chat, record, topics, changed) -> bool:
        """An open proposal now about ``topics``, its card redrawn with them and ``changed`` said on it; whether it was (False:
        decided meanwhile)."""
        state = Path(ctx.state_dir)
        topics = list(dict.fromkeys(topics))
        message = str((record.get("card") or {}).get("message_id") or "")
        view = {**(record.get("view") or self.proposal_view(state, record)), "changed": changed,
                "labels": [label_title(state, topic) for topic in topics]}
        if message:  # A row of an older 整理专项 card has no card of its own to draw.
            view.update(rows=self.topic_rows(ctx, chat, topics), choices=self.choices(ctx, chat, topics))
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
        view = {**(record.get("view") or self.proposal_view(state, record)), "state": proposals.EXPIRED, "note": note,
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
