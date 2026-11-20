"""The real-time half of 整理专项: one topic against the projects when its turn ended, the join a rule or the judge is sure of done at
once, and a new project's sweep (docs/projects.md 整理专项). Joins through app/tidy_apply.py, proposes through
app/tidy_proposals.py; the moments that call it are in app/project_tidy.py."""
from __future__ import annotations

from pathlib import Path

from covey.app import project_sorter as sorter
from covey.app import tidy_facts as facts
from covey.core import project_proposals as proposals
from covey.core import projects


class Realtime:
    """The real-time 整理 of one topic. ``runtime``: the log. ``env``: the judge's tools. ``shared``: app/tidy_state.py TidyState
    (``topic_count``). ``applier``: app/tidy_apply.py Applier (the joins). ``proposal_cards``: app/tidy_proposals.py
    ProposalCards (a new project's proposal)."""

    def __init__(self, runtime, env, shared, applier, proposal_cards) -> None:
        self.runtime, self.env, self.shared = runtime, env, shared
        self.applier, self.proposal_cards = applier, proposal_cards

    def handle(self, ctx, chat, topic, reason, now) -> str:
        """One topic's real-time 整理; what came of it in a word (for the log and the tests)."""
        state = Path(ctx.state_dir)
        if not proposals.enabled() or projects.topic_project(state, topic) is not None:
            return "skipped"
        if sorter.hidden(state, topic):
            return "redacted"
        records = facts.live_projects(state, chat)
        kept_out = proposals.never_auto(state, topic)
        if records and not kept_out:
            pid, why = facts.by_rule(state, chat, topic, records)
            if pid:
                outcome = self.rule_confirm(ctx, chat, topic, pid, why, now)
                self.runtime.log(f"project tidy realtime topic={topic[:12]} reason={reason} outcome={outcome} by=rule")
                if outcome == "joined" or reason == facts.TURN:  # This gist was judged just now: no second call for the turn.
                    return outcome
        if reason == facts.TURN:
            return "kept_out" if kept_out else self.turn_judge(ctx, chat, topic, records, now)
        if self.shared.topic_count(state, chat, now) < proposals.number("min_topics"):
            return "few"
        if proposals.open_with(state, topic):
            return "open"
        topics = [topic] + [other for other in facts.recent_topics(state, chat, now, records=records) if other != topic]
        backend, context = facts.judge_tools(self.env)
        # The topic judged is read whole first when it has no 话题要点 (a first title such as
        # 阅读这个session… says nothing): the judge then sees what it is about, and so does its proposal card.
        if sorter.needs_gist(state, topic, sorter.material(state, chat, topic, now).last_job):
            sorter.backfill(state, chat, topic, backend=backend, ctx=context, log=self.runtime.log, now=now)
        items = [sorter.material(state, chat, other, now) for other in topics[:sorter.MAX_TOPICS] if not sorter.hidden(state, other)]
        verdict = sorter.judge(state, records, items, backend=backend, ctx=context, focus=topic,
                               lessons=proposals.lessons(state), source="realtime", log=self.runtime.log)
        sign = facts.gist_sign(state, topic)
        proposals.change_topic(state, topic, lambda flags: flags.update(judged_at=now, judged_for=reason, judged_sign=sign))
        outcome = "none"
        for made in verdict.suggestions:
            if made["kind"] == proposals.JOIN and made["confidence"] >= proposals.JOIN_SURE and not proposals.never_auto(state, topic):
                if self.applier.join(ctx, made, chat):
                    outcome = "joined"
                    break
            elif made["kind"] == proposals.CREATE:
                outcome = self.proposal_cards.propose(ctx, chat, made) or outcome
                break
        self.runtime.log(f"project tidy realtime topic={topic[:12]} reason={reason} outcome={outcome} why={verdict.why}")
        return outcome

    # Joins without a click: the rule, each turn, a new project's sweep ---------------------------------------------------

    def rule_confirm(self, ctx, chat, topic, pid, why, now) -> str:
        """A rule hit (``by_rule``) joined only when the judge, reading this topic against that project alone, is sure of it
        (JOIN_SURE): ``related`` lists the topics a turn named, and a topic names others it is not part of (a
        disk check naming the topics whose tests filled the disk is not part of their project). "joined" or
        "rule_declined"; the gist counts as judged either way."""
        state = Path(ctx.state_dir)
        record = projects.read_record(state, pid)
        backend, context = facts.judge_tools(self.env)
        verdict = sorter.judge(state, [record], [sorter.material(state, chat, topic, now)], backend=backend, ctx=context,
                               focus=topic, lessons=proposals.lessons(state), source="realtime", log=self.runtime.log)
        sign = facts.gist_sign(state, topic)
        proposals.change_topic(state, topic, lambda flags: flags.update(judged_at=now, judged_for="rule", judged_sign=sign))
        sure = any(made["kind"] == proposals.JOIN and made["project"] == pid and made["confidence"] >= proposals.JOIN_SURE
                   for made in verdict.suggestions)
        return "joined" if sure and self.applier.join(ctx, facts.rule_join(topic, pid, why), chat, by="rule") else "rule_declined"

    def turn_judge(self, ctx, chat, topic, records, now) -> str:
        """After a turn the owner started: the judge reads this topic alone against the live projects (one call, only when its
        话题要点 changed since it was last judged) and a join it is sure of (JOIN_SURE) is done. Nothing is proposed here."""
        state = Path(ctx.state_dir)
        if not records:
            return "no_projects"
        sign = facts.gist_sign(state, topic)
        if not sign or proposals.topic_flags(state, topic).get("judged_sign") == sign:
            return "same"
        backend, context = facts.judge_tools(self.env)
        verdict = sorter.judge(state, records, [sorter.material(state, chat, topic, now)], backend=backend, ctx=context,
                               focus=topic, lessons=proposals.lessons(state), source="realtime", log=self.runtime.log)
        proposals.change_topic(state, topic, lambda flags: flags.update(judged_at=now, judged_for=facts.TURN, judged_sign=sign))
        outcome = "none"
        for made in verdict.suggestions:
            if made["kind"] == proposals.JOIN and made["confidence"] >= proposals.JOIN_SURE and self.applier.join(ctx, made, chat):
                outcome = "joined"
                break
        self.runtime.log(f"project tidy realtime topic={topic[:12]} reason={facts.TURN} outcome={outcome} why={verdict.why}")
        return outcome

    def sweep(self, ctx, chat, pid, now) -> int:
        """A project just made (app/projects.py queue_sweep) takes in the recent topics that belong to it: one judge call over
        them against this project alone (a rule hit among them too), the joins it is sure of. How many joined."""
        state = Path(ctx.state_dir)
        record = projects.read_record(state, pid)
        if not proposals.enabled() or record.get("state") not in (projects.ACTIVE, projects.PAUSED):
            return 0
        records = facts.live_projects(state, chat)
        joined, rest = 0, []
        for topic in facts.recent_topics(state, chat, now, records=records):
            if (projects.topic_project(state, topic) is not None or proposals.never_auto(state, topic)
                    or sorter.hidden(state, topic)):
                continue
            found, _ = facts.by_rule(state, chat, topic, records)
            if found in ("", pid):  # A rule hit is judged too (rule_confirm); one naming another project's topics is left out.
                rest.append(topic)
        if rest:
            backend, context = facts.judge_tools(self.env)
            items = [sorter.material(state, chat, topic, now) for topic in rest]
            verdict = sorter.judge(state, [record], items, backend=backend, ctx=context, lessons=proposals.lessons(state),
                                   source="sweep", log=self.runtime.log)
            for made in verdict.suggestions:
                if (made["kind"] == proposals.JOIN and made["project"] == pid and made["confidence"] >= proposals.JOIN_SURE
                        and self.applier.join(ctx, made, chat)):
                    joined += 1
        self.runtime.log(f"project tidy sweep project={pid[:8]} looked={len(rest)} joined={joined}")
        return joined


