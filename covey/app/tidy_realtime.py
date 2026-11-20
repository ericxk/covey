"""The real-time half of 整理专项: one topic against the projects when its turn ended, the join a rule or the judge is sure of done at
once, a new project's sweep, and the proposal of a new project (docs/butler.md 整理专项)."""
from __future__ import annotations

import re
import time
from pathlib import Path

from covey.app import project_sorter as sorter
from covey.app import topic_links
from covey.app.tidy_facts import TURN, gist_sign, labels
from covey.core import project_proposals as proposals
from covey.core import projects, topic_gist


class Realtime:
    """``ProjectTidy``'s real-time 整理. Uses the state of app/project_tidy.py ProjectTidy: ``runtime``, ``env``, ``counted``."""

    def realtime(self, ctx, chat, topic, reason, now) -> str:
        """One topic's real-time 整理; what came of it in a word (for the log and the tests)."""
        state = Path(ctx.state_dir)
        if not proposals.enabled() or projects.topic_project(state, topic) is not None:
            return "skipped"
        if sorter.hidden(state, topic):
            return "redacted"
        records = self.live_projects(state, chat)
        kept_out = proposals.never_auto(state, topic)
        if records and not kept_out:
            pid, why = self.by_rule(state, chat, topic, records)
            if pid:
                outcome = self.rule_confirm(ctx, chat, topic, pid, why, now)
                self.runtime.log(f"project tidy realtime topic={topic[:12]} reason={reason} outcome={outcome} by=rule")
                if outcome == "joined" or reason == TURN:  # This gist was judged just now: no second call for the turn.
                    return outcome
        if reason == TURN:
            return "kept_out" if kept_out else self.turn_judge(ctx, chat, topic, records, now)
        if self.topic_count(state, chat, now) < proposals.number("min_topics"):
            return "few"
        if proposals.open_with(state, topic):
            return "open"
        topics = [topic] + [other for other in self.recent_topics(state, chat, now, records=records) if other != topic]
        backend, context = self.judge_tools()
        # The topic judged is read whole first when it has no 话题要点 (the owner, 2026-11-18: T252's proposal only had its
        # first title, 阅读这个session…, to go on): the judge then sees what it is about, and so does its proposal card.
        if sorter.needs_gist(state, topic, sorter.material(state, chat, topic, now).last_job):
            sorter.backfill(state, chat, topic, backend=backend, ctx=context, log=self.runtime.log, now=now)
        items = [sorter.material(state, chat, other, now) for other in topics[:sorter.MAX_TOPICS] if not sorter.hidden(state, other)]
        verdict = sorter.judge(state, records, items, backend=backend, ctx=context, focus=topic,
                               lessons=proposals.lessons(state), source="realtime", log=self.runtime.log)
        sign = gist_sign(state, topic)
        proposals.change_topic(state, topic, lambda flags: flags.update(judged_at=now, judged_for=reason, judged_sign=sign))
        outcome = "none"
        for made in verdict.suggestions:
            if made["kind"] == proposals.JOIN and made["confidence"] >= proposals.JOIN_SURE and not proposals.never_auto(state, topic):
                if self.join(ctx, made, chat):
                    outcome = "joined"
                    break
            elif made["kind"] == proposals.CREATE:
                outcome = self.propose(ctx, chat, made) or outcome
                break
        self.runtime.log(f"project tidy realtime topic={topic[:12]} reason={reason} outcome={outcome} why={verdict.why}")
        return outcome

    # Joins without a click: the rule, each turn, a new project's sweep ---------------------------------------------------

    @staticmethod
    def by_rule(state, chat, topic, records) -> tuple:
        """(project id, why) when the 话题要点 of ``topic`` names in ``related`` topics of one live project of ``records``, and
        of no other; ('', '') otherwise. No model call."""
        related = topic_gist.gist_of(topic_gist.read(state, topic)).get("related") or ()
        numbers = topic_links.topic_numbers(state, chat)
        members = {other: record["id"] for record in records for other in record.get("threads") or ()}
        hits = {}
        for ref in related if isinstance(related, list) else ():
            found = re.fullmatch(r"[Tt](\d+)", str(ref).strip())
            other = numbers.get(int(found.group(1))) if found else None
            if other and other != topic and other in members:
                hits.setdefault(members[other], []).append(f"T{found.group(1)}")
        if len(hits) != 1:
            return "", ""
        pid, refs = next(iter(hits.items()))
        return pid, f"它的话题要点说和 {'、'.join(refs[:3])} 相关，那是这个专项的话题"

    @staticmethod
    def rule_join(topic, pid, why) -> dict:
        return proposals.new_proposal(proposals.JOIN, topics=[topic], project=pid, why=why, source="rule", confidence=1.0)

    def rule_confirm(self, ctx, chat, topic, pid, why, now) -> str:
        """A rule hit (``by_rule``) joined only when the judge, reading this topic against that project alone, is sure of it
        (JOIN_SURE): ``related`` lists the topics a turn named, and a topic names others it is not part of (T579, 2026-11-20: a
        disk check named the two topics whose tests had filled the disk and went into their 题包 project). "joined" or
        "rule_declined"; the gist counts as judged either way."""
        state = Path(ctx.state_dir)
        record = projects.read_record(state, pid)
        backend, context = self.judge_tools()
        verdict = sorter.judge(state, [record], [sorter.material(state, chat, topic, now)], backend=backend, ctx=context,
                               focus=topic, lessons=proposals.lessons(state), source="realtime", log=self.runtime.log)
        sign = gist_sign(state, topic)
        proposals.change_topic(state, topic, lambda flags: flags.update(judged_at=now, judged_for="rule", judged_sign=sign))
        sure = any(made["kind"] == proposals.JOIN and made["project"] == pid and made["confidence"] >= proposals.JOIN_SURE
                   for made in verdict.suggestions)
        return "joined" if sure and self.join(ctx, self.rule_join(topic, pid, why), chat, by="rule") else "rule_declined"

    def turn_judge(self, ctx, chat, topic, records, now) -> str:
        """After a turn the owner started: the judge reads this topic alone against the live projects (one call, only when its
        话题要点 changed since it was last judged) and a join it is sure of (JOIN_SURE) is done. Nothing is proposed here."""
        state = Path(ctx.state_dir)
        if not records:
            return "no_projects"
        sign = gist_sign(state, topic)
        if not sign or proposals.topic_flags(state, topic).get("judged_sign") == sign:
            return "same"
        backend, context = self.judge_tools()
        verdict = sorter.judge(state, records, [sorter.material(state, chat, topic, now)], backend=backend, ctx=context,
                               focus=topic, lessons=proposals.lessons(state), source="realtime", log=self.runtime.log)
        proposals.change_topic(state, topic, lambda flags: flags.update(judged_at=now, judged_for=TURN, judged_sign=sign))
        outcome = "none"
        for made in verdict.suggestions:
            if made["kind"] == proposals.JOIN and made["confidence"] >= proposals.JOIN_SURE and self.join(ctx, made, chat):
                outcome = "joined"
                break
        self.runtime.log(f"project tidy realtime topic={topic[:12]} reason={TURN} outcome={outcome} why={verdict.why}")
        return outcome

    def sweep(self, ctx, chat, pid, now) -> int:
        """A project just made (app/projects.py queue_sweep) takes in the recent topics that belong to it: one judge call over
        them against this project alone (a rule hit among them too), the joins it is sure of. How many joined."""
        state = Path(ctx.state_dir)
        record = projects.read_record(state, pid)
        if not proposals.enabled() or record.get("state") not in (projects.ACTIVE, projects.PAUSED):
            return 0
        records = self.live_projects(state, chat)
        joined, rest = 0, []
        for topic in self.recent_topics(state, chat, now, records=records):
            if (projects.topic_project(state, topic) is not None or proposals.never_auto(state, topic)
                    or sorter.hidden(state, topic)):
                continue
            found, _ = self.by_rule(state, chat, topic, records)
            if found in ("", pid):  # A rule hit is judged too (rule_confirm); one naming another project's topics is left out.
                rest.append(topic)
        if rest:
            backend, context = self.judge_tools()
            items = [sorter.material(state, chat, topic, now) for topic in rest]
            verdict = sorter.judge(state, [record], items, backend=backend, ctx=context, lessons=proposals.lessons(state),
                                   source="sweep", log=self.runtime.log)
            for made in verdict.suggestions:
                if (made["kind"] == proposals.JOIN and made["project"] == pid and made["confidence"] >= proposals.JOIN_SURE
                        and self.join(ctx, made, chat)):
                    joined += 1
        self.runtime.log(f"project tidy sweep project={pid[:8]} looked={len(rest)} joined={joined}")
        return joined

    def propose(self, ctx, chat, made) -> str:
        """A new project the judge sees: made at once (``auto``, or the owner's 以后直接建), else a proposal card; '' when it
        is not proposed (以后别提议, turned down before, MAX_ACTIVE projects on)."""
        state = Path(ctx.state_dir)
        prefs = proposals.prefs(state)
        # Topics already in a project are not proposed again (the owner, 2026-11-19: 已经归进专项的话题还挂在提议卡上).
        topics = proposals.free(state, made["topics"])
        if len(topics) < proposals.MIN_CREATE:
            return ""
        if topics != list(made["topics"]):
            made = {**made, "topics": topics, "id": proposals.proposal_id(proposals.CREATE, topics)}
        if projects.active_count(state) >= projects.MAX_ACTIVE:
            return ""
        if proposals.declined_like(state, proposals.CREATE, made["topics"]):
            return ""
        same = proposals.overlapping(state, topics)
        if same:
            # Mostly the topics of a proposal already waiting for him: no second card; its topics it lacks go onto that card.
            more = [topic for topic in topics if topic not in (same.get("topics") or ())]
            if not more:
                return ""
            note = f"又发现 {labels(state, more)} 也是这件事，补进来了。"
            merged = proposals.free(state, [*same["topics"], *more])
            return "merged" if self.retopic(ctx, chat, same, merged, note) else ""
        if proposals.mode() == proposals.AUTO or prefs.get("auto_create"):
            saved, _ = proposals.put_proposal(state, {**made, "state": proposals.ACCEPTED, "decided_at": time.time()})
            if saved.get("state") != proposals.ACCEPTED:
                return ""
            pid = self.create(ctx, chat, saved, owner_named=False)
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

    def gists(self, ctx, chat, topics) -> None:
        """The 话题要点 of each of ``topics`` that has none, so its proposal card says what each is about (one read a topic;
        one that cannot be read keeps its title on the card)."""
        state, now = Path(ctx.state_dir), time.time()
        missing = [topic for topic in topics if not sorter.hidden(state, topic)
                   and sorter.needs_gist(state, topic, sorter.material(state, chat, topic, now).last_job)]
        if not missing:
            return
        backend, context = self.judge_tools()
        for topic in missing:
            sorter.backfill(state, chat, topic, backend=backend, ctx=context, log=self.runtime.log, now=now)
