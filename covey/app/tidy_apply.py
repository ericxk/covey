"""Doing what 整理专项 decided: a project made, a join, a leave, a merge, a split, a fold, and the topic's cards marked 已归进.
Takes no click and draws no proposal or 整理专项 card (docs/butler.md 整理专项); it depends on app/tidy_facts.py alone, and the
parts that ask for it (app/tidy_proposals.py, app/tidy_realtime.py, app/tidy_run.py, app/tidy_clicks.py) come above it."""
from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

from covey.app import tidy_facts as facts
from covey.cards import project_proposal as cards
from covey.core import project_proposals as proposals
from covey.core import projects
from covey.core.card_sequence import file_card, reserve
from covey.core.locks import resource_lock
from covey.core.state import read_object, write_json
from covey.core.topics import label_title, topic_file


class Applier:
    """Does what was decided. ``runtime``: the log and the owner's chat. ``messenger``, ``voice``: the cards it marks. ``env``:
    app/butler_env.py ButlerEnv (``projects`` makes a project, ``topics`` and ``state_dir`` and ``profile`` mark the cards)."""

    def __init__(self, runtime, messenger, voice, env) -> None:
        self.runtime, self.messenger, self.voice, self.env = runtime, messenger, voice, env

    # Doing -------------------------------------------------------------------------------------------------------------

    def create(self, ctx, chat, record, *, owner_named, name="", goal="") -> str:
        projects_ = getattr(self.env, "projects", None)
        if projects_ is None:
            return ""
        return projects_.gather(chat=chat, name=name or record.get("name"), goal=goal or record.get("goal") or "",
                                topics=record.get("topics") or (), key=record["id"], why=record.get("why") or "",
                                owner_named=owner_named)

    def join(self, ctx, made, chat, *, by="tidy") -> bool:
        """A sure join done (app/project_members.py join_topic; ``by`` "rule" when the 话题要点 decided it) and the topic's cards marked;
        whether it was."""
        from covey.app.project_members import join_topic

        state, topic, pid = Path(ctx.state_dir), made["topics"][0], made["project"]
        try:
            record = join_topic(state, pid, topic, why=made.get("why") or "", by=by)
        except (ValueError, KeyError) as exc:
            self.runtime.log(f"project tidy join skipped topic={topic[:12]}: {exc}")
            return False
        proposals.put_proposal(state, {**made, "state": proposals.DONE, "decided_at": time.time()})
        proposals.change_topic(state, topic, lambda flags: flags.update(joined={"project": pid, "at": time.time(), "by": by}))
        self.mark(ctx, chat, topic, pid, record.get("name") or "")
        self.runtime.log(f"project tidy joined topic={topic[:12]} project={pid[:8]}")
        return True

    def leave(self, ctx, chat, topic, pid, *, why="") -> str:
        """移出: the topic an ordinary one again, kept out for good, its cards' line taken away; the words said."""
        from covey.app.project_members import leave_topic

        state = Path(ctx.state_dir)
        name = projects.read_record(state, pid).get("name") or "专项"
        if not leave_topic(state, pid, topic, why=why):
            return f"这个话题已经不在专项「{name}」里了。"
        proposals.change_topic(state, topic, lambda flags: flags.update(never_auto=True, left=[*(flags.get("left") or []),
                                                                                              {"project": pid, "at": time.time()}]))
        self.mark(ctx, chat, topic, pid, "")
        folded = self.fold_if_empty(state, pid)
        return f"已把这个话题移出专项「{name}」，以后它不会再被自动归进专项。" + (f"专项「{name}」没有话题了，已收起。" if folded else "")

    def apply(self, ctx, chat, record) -> tuple:
        """Do an accepted proposal: (ok, the line said)."""
        from covey.app.project_members import join_topic, leave_topic

        state, kind, pid = Path(ctx.state_dir), record["kind"], record.get("project") or ""
        if kind == proposals.CREATE:
            if projects.active_count(state) >= projects.MAX_ACTIVE:
                return False, f"已经有 {projects.MAX_ACTIVE} 个专项在做，没建：先收尾或停掉一个"
            made = self.create(ctx, chat, record, owner_named=False)
            return (True, f"已建专项「{record.get('name')}」，专项卡在聊天区") if made else (False, "专项没建成")
        if kind == proposals.JOIN:
            ok = self.join(ctx, record, chat)
            return ok, "已归进" if ok else "没归进去：话题或专项已经变了"
        if kind == proposals.LEAVE:
            return True, self.leave(ctx, chat, record["topics"][0], pid)
        current = projects.read_record(state, pid)
        if not current or current.get("state") not in (projects.ACTIVE, projects.PAUSED):
            return False, "这个专项已经结束了"
        if kind == proposals.RENAME:
            if proposals.owner_named(current):
                return False, "这个名字是你取的，不改"
            projects.update(state, pid, lambda data: data.update(name=record["name"]))
            return True, f"已改名「{record['name']}」"
        if kind == proposals.FOLD:
            self.fold(state, pid)
            return True, f"已收起「{current.get('name')}」"
        if kind == proposals.MERGE:
            kept = record["projects"][0]
            into = f"随合并转进专项「{facts.project_name(state, kept)[:30]}」"
            for other in record["projects"][1:]:
                gone = projects.read_record(state, other)
                if not gone or gone.get("state") not in (projects.ACTIVE, projects.PAUSED):
                    continue
                for topic in list(gone.get("threads") or ()):
                    leave_topic(state, other, topic, went=into)
                    try:
                        join_topic(state, kept, topic, why="从合并的专项转来")
                    except ValueError:
                        pass
                from covey.app.project_cards import MERGED
                projects.set_state(state, other, projects.CANCELLED, MERGED)
            if record.get("name") and not proposals.owner_named(current):
                projects.update(state, kept, lambda data: data.update(name=record["name"]))
            return True, f"已合并进「{projects.read_record(state, kept).get('name')}」"
        if kind == proposals.SPLIT:
            # Only the topics still the project's go (T574: one the owner took out, or another proposal moved, stays where it
            # is), and two at least, as the judge was allowed to propose (app/project_sorter.py MIN_CREATE).
            movable, gone = facts.still_in(state, record["topics"], pid)
            if len(movable) < proposals.MIN_CREATE:
                return False, f"已过时：{gone + '，' if gone else ''}剩下的不够另建一个专项，没拆"
            if projects.active_count(state) >= projects.MAX_ACTIVE:
                return False, f"已经有 {projects.MAX_ACTIVE} 个专项在做，没拆"
            name = str(record.get("name") or "")
            for topic in movable:
                leave_topic(state, pid, topic, went=f"拆去了新专项「{name[:30]}」")
            made = self.create(ctx, chat, {**record, "topics": movable}, owner_named=False)
            if not made:
                return False, "没拆成"
            words = f"已拆出专项「{name}」" + (f"（{gone}，没带上）" if gone else "")
            if self.fold_if_empty(state, pid):
                words += f"；「{current.get('name')}」的话题都拆走了，已收起"
            return True, words
        return False, "不认识的建议"

    def fold_if_empty(self, state, pid) -> bool:
        """收起 a project 整理专项 or 移出 left without topics, when nothing of it runs (the owner, 2026-11-20: 只剩空壳就马上收起;
        his word in it opens it again, app/project_loop.py reopen); whether it was."""
        record = projects.read_record(state, pid)
        if not record or record.get("threads") or not facts.foldable(state, pid):
            return False
        self.fold(state, pid)
        return projects.folded(projects.read_record(state, pid))

    @staticmethod
    def fold(state, pid) -> None:
        """收起: a project with nothing to do, folded away as the idle fold does it (app/project_loop.py rest)."""
        def change(data):
            if data.get("state") == projects.ACTIVE:
                data["state"], data["state_reason"] = projects.PAUSED, projects.IDLE
                data["idle"] = {"at": time.time(), "days": 0, "by": "tidy"}
                data.pop("stall", None)
        projects.update(state, pid, change)

    # The topic's cards ---------------------------------------------------------------------------------------------

    def show_joined(self, topic, pid) -> None:
        """The topic's cards marked 已归进「X」 · 移出 after a join made elsewhere (a direct start's ``topics``, the lead's send),
        so the owner can take it back with one click as he can a join of 整理专项; never raises."""
        ctx = SimpleNamespace(state_dir=Path(self.env.state_dir), profile=self.env.profile)
        try:
            chat = facts.owner_chat(self.runtime)
            name = projects.read_record(ctx.state_dir, pid).get("name") or ""
            if chat and name:
                self.mark(ctx, chat, topic, pid, name)
        except Exception as exc:  # noqa: BLE001 - the 归入记录 on the project's card still says it
            self.runtime.log(f"project tidy mark failed topic={topic[:12]}: {type(exc).__name__}")

    def mark(self, ctx, chat, topic, pid, name) -> None:
        """The topic card and the status card of ``topic`` drawn with 已归进「name」 · 移出 (``name`` '': without it); a topic that has
        neither gets the line as a card of its own in its thread (once)."""
        joined = cards.joined_element(name, topic, pid) if name else None
        drawn = self.topic_card(ctx, topic, joined)
        drawn = self.status_card(ctx, topic, [name, topic, pid] if name else None) or drawn
        if name and not drawn:
            root = str(read_object(topic_file(Path(ctx.state_dir), topic)).get("root_id") or "")
            if root.startswith("om_"):
                self.messenger.send_message(ctx.profile, chat, f"已归进专项「{name}」",
                                            self.voice.safe_key(self.voice.key("tidy-joined", f"{topic[:16]}-{pid}")),
                                            reply_to_message_id=root, voice=self.voice, card=cards.joined_card(name, topic, pid))

    def topic_card(self, ctx, topic, joined) -> bool:
        """The butler's topic card of ``topic`` (app/butler_topics.py) redrawn whole with ``joined`` (None: without); whether it was."""
        from covey.app import butler_topics
        from covey.cards.topic_card import topic_card

        state = Path(ctx.state_dir)
        origin = read_object(topic_file(state, topic)).get("origin") or {}
        request = origin.get("request") if isinstance(origin, dict) else None
        if not request:
            return False
        path = butler_topics.record_path(state, type("Request", (), {"id": request})())
        record = read_object(path)
        if not record.get("card_id") or record.get("topic_id") != topic:
            return False
        label, _ = label_title(state, topic)
        try:
            config = butler_topics.config_line(self.env, record["plan"]) if record.get("plan") else ()
        except Exception:  # noqa: BLE001 - the line without the configuration
            config = ()
        built = topic_card(record.get("title") or "", label=label, config=config, joined=joined)
        handle = file_card(path, record, shared=False)
        with handle.lock:
            sequence = reserve(handle)
            result = self.messenger.put_card(ctx.profile, record["card_id"], built, sequence=sequence,
                                             key=f"main-topic-{path.stem[:24]}-{sequence}", timeout=3)
        return result is not None

    def status_card(self, ctx, topic, joined) -> bool:
        """The topic's status card (app/status_cards.py) written once with ``joined`` as its last line; whether it was."""
        from covey.app import status_cards
        from covey.cards.status_card import status_card

        path = status_cards.record_path(ctx.state_dir, topic)
        record = read_object(path)
        shown = record.get("shown")
        if not record.get("card_id") or not record.get("message_id") or not isinstance(shown, list) or len(shown) < 4:
            return False
        now_shown = [*shown[:4], *([joined] if joined else [])]
        handle = file_card(path, record, shared=False)
        with handle.lock:
            sequence = reserve(handle)
            result = self.messenger.put_card(ctx.profile, record["card_id"], status_card(*now_shown), sequence=sequence,
                                             key=f"status-{topic[:16]}-{sequence}", timeout=10)
        if result is None:
            return False
        with resource_lock("status-card", str(path)):  # The board's loop writes the record under the same lock.
            write_json(path, {**read_object(path), "shown": now_shown, "written_at": time.time()}, ensure_ascii=False, indent=1)
        return True
