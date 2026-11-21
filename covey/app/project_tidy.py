"""整理专项 (docs/projects.md 整理专项, COVEY_PROJECT_TIDY=suggest|auto with COVEY_PROJECTS=1, off by default): projects grow out of the
topics by a mechanism, not by the one look the butler takes when it opens a topic.

- **话题要点**: an ordinary topic's (and project topic's) turn ends with its own model writing ``topic_gist`` in its checkpoint
  when the topic has none or its goal or scope moved (app/runs.py puts the rule and the topic's current gist in each turn's
  message). The thread here (``tick``, every TICK seconds) reads the checkpoints of the turns that ended since it last looked
  (``scan``: the jobs' index, ``cursor``) and keeps each gist written in the topic's gist.json (core/topic_gist.py); a turn
  that wrote none leaves the last one.
- **Real time** (``realtime``), for one topic against the projects and the topics of the last COVEY_PROJECT_TIDY_DAYS days: a new
  topic's first turn ended; its essence changed (core/topic_gist.py ``moved``); its turn ended on its third day or later
  (COVEY_PROJECT_TIDY_AGE_DAYS), or it has COVEY_PROJECT_TIDY_WORDS turns the owner started. Each of the last two once a topic.
  自动收话题: a join needs no click. The rule first (``by_rule``: its 话题要点's related topics are one
  live project's, a candidate the judge confirms against that project alone, ``rule_confirm``); each turn the owner started
  (TURN) the judge reads this topic alone against the live projects when its gist changed (``turn_judge``); a project just
  made looks over the recent topics once (``sweep``).
  A join the judge is sure of (JOIN_SURE) is done at once (app/project_members.py join_topic) and the topic's cards get 已归进「X」 · 移出;
  a new project is proposed on a card (建专项 / 改个名再建 / 不用 / 以后别提议), or made at once with ``auto`` (or after the owner's
  以后直接建). Not with fewer than COVEY_PROJECT_TIDY_MIN_TOPICS topics in the chat, not for a topic taken out of a project (移出)
  as far as joining goes, never twice for an open proposal.
- **Proposals follow the joins** (``follow``, each tick): an open proposal some of whose topics are now in
  a project, however they got there, keeps the rest (its card redrawn) or, with fewer than MIN_CREATE left, ends 已过时 with
  where they went. A new 新建 leaves out the topics already in a project and goes onto the card of a waiting proposal it mostly
  overlaps (core/project_proposals.py ``overlapping``) instead of a card of its own.
- **Scheduled and asked for** (``start``): with the 早晚汇总 (COVEY_DIGEST_TIMES, the same times, a record of its own), the
  butler's projects_tidy, the 设置 card's 整理专项 and the web page's ``tidy``. One at a time (``busy``): asked while one runs,
  the answer is where it is. The 整理专项 card goes out first and shows 正在读 T88（12/40） while the topics without a 话题要点
  are read (app/project_sorter.py backfill: one call a topic, one that cannot be read sits out and is counted); then one judge
  call, and the card is redrawn with what was done at once (sure joins, folding an idle project) and at most CARD_ROWS
  suggestions, each with 好 / 不用, and 全部照办.

A decision is kept (core/project_proposals.py): a proposal turned down comes back only with new evidence, 以后别提议 stops
proposals (the butler still opens a project when the owner asks), 移出 keeps a topic out for good, the owner's names are never
changed, and the reasons he gives under a 不用 or a 移出 are the judge's rules from then on. Rules 1 and 3: no lock is held
across a model or Feishu call; cards here are message cards replaced whole (patch_card), the topic card and the status card
written with their own records' sequence.

ProjectTidy is the coordinator: it holds the parts as attributes, made in ``__init__`` (their constructors' arguments are what each
part depends on), and what they share (``shared``); the parts call down that list and never back (tests/test_structure.py
TidyPartsTest), and none inherits from another. It keeps the thread (``tick``), 话题要点 and the moments (``scan``, ``saw``, ``drain``)
itself, and the methods other modules call (``start``, ``claim``, ``show_joined``, ``web_*``) hand on to the part that has them.

- app/tidy_facts.py: plain functions of the records, the bottom: words for topics and projects, 话题要点's sign, the topics a 整理
  looks at, the proposal card's view, the claim of an open proposal, the rows' words.
- app/tidy_state.py: ``TidyState``, what is shared: ``busy``, ``progress``, ``counted``, ``handed``.
- app/tidy_words.py: ``TopicWords``, topics as the cards word them (the one part that asks Feishu for a link).
- app/tidy_apply.py: ``Applier``, doing what was decided (project, join, leave, merge, split, fold) and marking the topic's cards.
- app/tidy_proposals.py: ``ProposalCards``, proposing a new project, the proposal card and its following the joins (``follow``).
- app/tidy_realtime.py: ``Realtime``, the real-time 整理 of a topic and a new project's sweep.
- app/tidy_run.py: ``Runs``, a whole scheduled or asked-for 整理 and the 整理专项 card.
- app/tidy_clicks.py: ``Clicks``, the clicks on the cards (``claim`` is the entry app/unified.py PANELS calls).
- app/tidy_web.py: ``WebSide``, the web page's panel and its 好 / 不用."""
from __future__ import annotations

import time
from pathlib import Path

from covey.app import tidy_facts as facts
from covey.app.tidy_apply import Applier
from covey.app.tidy_clicks import Clicks
from covey.app.tidy_proposals import ProposalCards
from covey.app.tidy_realtime import Realtime
from covey.app.tidy_run import Runs
from covey.app.tidy_state import TidyState
from covey.app.tidy_web import WebSide
from covey.app.tidy_words import TopicWords
from covey.core import project_proposals as proposals
from covey.core import projects, topic_gist
from covey.core.butler import is_butler_topic
from covey.core.checkpoint import checkpoint_gist, read_checkpoint
from covey.core.jobs import job_key, jobs_dir, select_job_records
from covey.core.loops import every
from covey.core.state import epoch_seconds
from covey.core.store import directory_store

TICK = 20
START_WAIT = 30
QUEUE_MAX = proposals.QUEUE_MAX


class ProjectTidy:
    """The Bridge's feature object (app/unified.py ``project_tidy``): one thread with the switch on, none without, and the
    claim of its cards' clicks (PANELS). ``env``: app/butler_env.py ButlerEnv (``projects`` the projects' feature object)."""
    name = "project_tidy"

    def __init__(self, runtime, messenger, voice, env) -> None:
        self.runtime, self.messenger, self.voice, self.env = runtime, messenger, voice, env
        self.shared = TidyState()
        # The parts, each below the ones after it; a part's constructor arguments are what it depends on.
        self.words = TopicWords(runtime)
        self.applier = Applier(runtime, messenger, voice, env)
        self.proposal_cards = ProposalCards(runtime, messenger, voice, env, self.words, self.applier)
        self.realtime = Realtime(runtime, env, self.shared, self.applier, self.proposal_cards)
        self.runs = Runs(runtime, messenger, voice, env, self.shared, self.words, self.applier, self.proposal_cards)
        self.clicks = Clicks(runtime, messenger, voice, self.shared, self.words, self.applier, self.proposal_cards, self.runs)
        self.web = WebSide(runtime, env, self.shared, self.words, self.clicks)

    # The thread ------------------------------------------------------------------------------------------------------

    def threads(self):
        return ((self.name, self.run),) if proposals.enabled() else ()

    def run(self, ctx) -> None:
        if self.runtime.shutdown.wait(START_WAIT):
            return
        every(self.runtime, TICK, self.tick, args=(ctx,), isolate=self.name)

    def chat(self) -> str:
        return facts.owner_chat(self.runtime)

    def tick(self, ctx, now=None) -> None:
        if not proposals.enabled():
            return
        now = time.time() if now is None else now
        state, chat = Path(ctx.state_dir), self.chat()
        if not chat:
            return
        fired = self.scan(state, chat, now)
        if fired:
            def queue(data):
                data["queue"] = [*(data.get("queue") or []), *({"topic": topic, "reason": why, "at": now} for topic, why in fired)
                                 ][-QUEUE_MAX:]
            proposals.change(state, queue)
        self.proposal_cards.follow(ctx, chat)
        self.runs.scheduled(ctx, chat, now)
        if not self.shared.busy.locked():
            self.drain(ctx, chat, now)

    # 话题要点 and the moments ---------------------------------------------------------------------------------------------

    def scan(self, state, chat, now) -> list:
        """The gists of the turns that ended since the last look, kept; the (topic, reason) pairs that call for a real-time 整理.
        The first look only sets the cursor: turns from before are not read."""
        data = proposals.load(state)
        cursor = data.get("cursor")
        if not isinstance(cursor, (int, float)):
            proposals.change(state, lambda data: data.update(cursor=now, since=data.get("since") or now, seen=[]))
            return []
        seen = set(data.get("seen") or ())

        def recent(fields):
            stamp = epoch_seconds(fields.get("updated_at"))
            return stamp is None or stamp >= cursor - 2

        jobs = select_job_records(state, lambda record: facts.ended(record) and recent(record), chat_id=chat, narrow=recent)
        fired, newest, keys = [], cursor, []
        for record in sorted(jobs, key=lambda record: (epoch_seconds(record.get("updated_at")) or 0, int(record.get("job_id") or 0))):
            key = job_key(str(record.get("message_id") or ""), record.get("job_id") or "")
            stamp = epoch_seconds(record.get("updated_at")) or now
            newest = max(newest, stamp)
            if key in seen:
                continue
            keys.append(key)
            topic = str(record.get("topic_id") or "")
            if not projects.TOPIC_RE.fullmatch(topic) or is_butler_topic(topic, chat):
                continue
            try:
                fired += [(topic, why) for why in self.saw(state, chat, topic, record, data, now)]
            except Exception as exc:  # noqa: BLE001 - one topic's records do not stop the others
                self.runtime.log(f"project tidy scan failed topic={topic[:12]}: {type(exc).__name__}")

        def move(data):
            data["cursor"] = newest
            data["seen"] = [*(data.get("seen") or []), *keys][-proposals.SEEN_KEPT:]
        proposals.change(state, move)
        return list(dict.fromkeys(fired))  # Two turns of one topic since the last look: one real-time 整理.

    def saw(self, state, chat, topic, record, data, now) -> list:
        """One ended turn: its gist kept; the reasons it gives for a real-time 整理 (each moment once a topic, the essence each time)."""
        why = []
        path = str(record.get("checkpoint_path") or "")
        raw = checkpoint_gist(read_checkpoint(Path(path))) if path else None
        if raw is not None:
            _, moved = topic_gist.save(state, topic, raw, source="turn", job=str(record.get("message_id") or ""), now=now)
            if moved:
                why.append("essence")
        if projects.topic_project(state, topic) is not None:
            return []  # A project's topic (or a lead): 整理 at its times only.
        jobs = list(directory_store(jobs_dir(state)).query(topic_id=topic, chat_id=chat))
        first = min((epoch_seconds(job.get("created_at")) or now for _, job in jobs), default=now)
        owner_turns = sum(1 for _, job in jobs if job.get("user_text"))
        flags = proposals.topic_flags(state, topic)
        fired = flags.get("fired") if isinstance(flags.get("fired"), dict) else {}
        since = float(data.get("since") or 0)
        if "new" not in fired and first >= since:
            why.append("new")
        if "age" not in fired and now - first >= proposals.number("age_days") * 86400:
            why.append("age")
        if "words" not in fired and owner_turns >= proposals.number("words"):
            why.append("words")
        if record.get("user_text"):
            why.append(facts.TURN)  # Each turn the owner started: may it join a project now (``realtime``, once a 话题要点).
        once = [reason for reason in why if reason not in ("essence", facts.TURN)]
        if once:
            proposals.change_topic(state, topic, lambda flags: flags.update(fired={**(flags.get("fired") or {}),
                                                                                    **{reason: now for reason in once}}))
        return why

    def drain(self, ctx, chat, now) -> None:
        """The real-time 整理 the scan queued, each topic once."""
        state = Path(ctx.state_dir)
        taken = []
        proposals.change(state, lambda data: (taken.extend(data.get("queue") or []), data.update(queue=[])))
        done = set()
        for item in taken:
            if item.get("project"):
                try:
                    self.realtime.sweep(ctx, chat, str(item["project"]), now)
                except Exception as exc:  # noqa: BLE001 - the topics' own turns still bring them in
                    self.runtime.log(f"project tidy sweep failed project={str(item['project'])[:8]}: {type(exc).__name__}")
                continue
            topic = str(item.get("topic") or "")
            if topic in done or not projects.TOPIC_RE.fullmatch(topic):
                continue
            done.add(topic)
            try:
                self.realtime.handle(ctx, chat, topic, str(item.get("reason") or ""), now)
            except Exception as exc:  # noqa: BLE001 - the next moment tries again
                self.runtime.log(f"project tidy realtime failed topic={topic[:12]}: {type(exc).__name__}")

    # What other modules call ----------------------------------------------------------------------------------------------

    @property
    def handed(self) -> dict:
        """BOARD_FROM: the 看板 redrawn after a proposal decided on it (app/unified.py fills it): the one dict in ``shared``."""
        return self.shared.handed

    def start(self, ctx=None, *, origin="asked", scope="", wait=False) -> str:
        """A whole 整理 now (app/tidy_run.py Runs.start): butler_projects.py projects_tidy, the web page's ``tidy``."""
        return self.runs.start(ctx, origin=origin, scope=scope, wait=wait)

    @staticmethod
    def matches(raw):
        """Whether a click is one of 整理专项's cards': every controller in app/unified.py PANELS answers it (and ``claim``)."""
        return Clicks.matches(raw)

    def claim(self, ctx, event, raw, owner):
        """A click on one of 整理专项's cards (app/tidy_clicks.py Clicks.claim; app/unified.py PANELS), else None."""
        return self.clicks.claim(ctx, event, raw, owner)

    def show_joined(self, topic, pid) -> None:
        """The topic's cards marked 已归进「X」 after a join made elsewhere (app/tidy_apply.py Applier.show_joined)."""
        self.applier.show_joined(topic, pid)

    def web_stamps(self) -> tuple:
        return self.web.stamps()

    def web_view(self, now=None) -> dict | None:
        return self.web.view(now)

    def web_decide(self, pid, choice) -> tuple:
        return self.web.decide(pid, choice)
