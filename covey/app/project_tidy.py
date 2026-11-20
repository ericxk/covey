"""整理专项 (docs/butler.md 整理专项, COVEY_PROJECT_TIDY=suggest|auto with COVEY_PROJECTS=1, off by default): projects grow out of the
topics by a mechanism, not by the one look the butler takes when it opens a topic.

- **话题要点**: every ordinary topic's (and project topic's) turn ends with its own model writing ``topic_gist`` in its checkpoint
  (app/runs.py puts the rule and the topic's current gist in each turn's message). The thread here (``tick``, every TICK
  seconds) reads the checkpoints of the turns that ended since it last looked (``scan``: the jobs' index, ``cursor``) and keeps
  each gist in the topic's gist.json (core/topic_gist.py).
- **Real time** (``realtime``), for one topic against the projects and the topics of the last COVEY_PROJECT_TIDY_DAYS days: a new
  topic's first turn ended; its essence changed (core/topic_gist.py ``moved``); its turn ended on its third day or later
  (COVEY_PROJECT_TIDY_AGE_DAYS), or it has COVEY_PROJECT_TIDY_WORDS turns the owner started. Each of the last two once a topic.
  自动收话题 (the owner, 2026-11-19): a join needs no click. The rule first (``by_rule``: its 话题要点's related topics are one
  live project's, a candidate the judge confirms against that project alone, ``rule_confirm``); each turn the owner started
  (TURN) the judge reads this topic alone against the live projects when its gist changed (``turn_judge``); a project just
  made looks over the recent topics once (``sweep``).
  A join the judge is sure of (JOIN_SURE) is done at once (app/project_members.py join_topic) and the topic's cards get 已归进「X」 · 移出;
  a new project is proposed on a card (建专项 / 改个名再建 / 不用 / 以后别提议), or made at once with ``auto`` (or after the owner's
  以后直接建). Not with fewer than COVEY_PROJECT_TIDY_MIN_TOPICS topics in the chat, not for a topic taken out of a project (移出)
  as far as joining goes, never twice for an open proposal.
- **Proposals follow the joins** (``follow``, each tick; the owner, 2026-11-19): an open proposal some of whose topics are now in
  a project, however they got there, keeps the rest (its card redrawn) or, with fewer than MIN_CREATE left, ends 已过时 with
  where they went. A new 新建 leaves out the topics already in a project and goes onto the card of a waiting proposal it mostly
  overlaps (core/project_proposals.py ``overlapping``) instead of a card of its own.
- **Scheduled and asked for** (``start``): with the 早晚汇总 (COVEY_DIGEST_TIMES, the same times, a record of its own), the
  butler's projects_tidy, the 设置 card's 整理专项 and the web page's ``tidy``. One at a time (``busy``): asked while one runs,
  the answer is where it is. The 整理专项 card goes out first and shows 正在读 T488（12/40） while the topics without a 话题要点
  are read (app/project_sorter.py backfill: one call a topic, one that cannot be read sits out and is counted); then one judge
  call, and the card is redrawn with what was done at once (sure joins, folding an idle project) and at most CARD_ROWS
  suggestions, each with 好 / 不用, and 全部照办.

A decision is kept (core/project_proposals.py): a proposal turned down comes back only with new evidence, 以后别提议 stops
proposals (the butler still opens a project when the owner asks), 移出 keeps a topic out for good, the owner's names are never
changed, and the reasons he gives under a 不用 or a 移出 are the judge's rules from then on. Rules 1 and 3: no lock is held
across a model or Feishu call; cards here are message cards replaced whole (patch_card), the topic card and the status card
written with their own records' sequence.

ProjectTidy is made of the modules below, each a part of it (a mixin: they share its state, ``runtime``, ``messenger``, ``voice``,
``env``, ``busy``, ``progress``, and call each other's methods through ``self``); app/tidy_facts.py is plain functions they all use.

- app/project_tidy.py: the thread (``tick``), 话题要点 and the moments (``scan``, ``saw``, ``drain``), reading the topics.
- app/tidy_realtime.py: ``Realtime``, the real-time 整理 and a new project's proposal (``propose``).
- app/tidy_proposals.py: ``ProposalCards``, the proposal card and its following the joins (``follow``).
- app/tidy_apply.py: ``Apply``, doing what was decided (project, join, leave, merge, split, fold) and marking the topic's cards.
- app/tidy_run.py: ``Runs``, a whole scheduled or asked-for 整理 and the 整理专项 card.
- app/tidy_clicks.py: ``Clicks``, the clicks on the cards (``claim`` is the entry app/unified.py PANELS calls).
- app/tidy_web.py: ``WebSide``, the web page's panel and its 好 / 不用.
- app/tidy_facts.py: words for topics and projects, 话题要点's sign, the claim of an open proposal."""
from __future__ import annotations

import threading
import time
from pathlib import Path

from covey.app import project_sorter as sorter
from covey.app import topic_links
from covey.app.tidy_apply import Apply
from covey.app.tidy_clicks import Clicks
from covey.app.tidy_facts import TURN, ended
from covey.app.tidy_proposals import ProposalCards
from covey.app.tidy_realtime import Realtime
from covey.app.tidy_run import Runs
from covey.app.tidy_web import WebSide
from covey.core import project_proposals as proposals
from covey.core import projects, topic_gist
from covey.core.butler import is_butler_topic
from covey.core.checkpoint import checkpoint_gist, read_checkpoint
from covey.core.jobs import job_key, jobs_dir, select_job_records
from covey.core.loops import every
from covey.core.state import epoch_seconds
from covey.core.store import directory_store
from covey.core.topics import label_title

TICK = 20
START_WAIT = 30
COUNT_KEPT = 600.0  # The chat's topic count is read again after this.
QUEUE_MAX = proposals.QUEUE_MAX


class ProjectTidy(Realtime, ProposalCards, Apply, Runs, Clicks, WebSide):
    """The Bridge's feature object (app/unified.py ``project_tidy``): one thread with the switch on, none without, and the
    claim of its cards' clicks (PANELS). ``env``: app/butler_env.py ButlerEnv (``projects`` the projects' feature object)."""
    name = "project_tidy"

    link = staticmethod(topic_links.link_for)  # A topic's applink for the proposal card (tests stand in for it).

    def __init__(self, runtime, messenger, voice, env) -> None:
        self.runtime, self.messenger, self.voice, self.env = runtime, messenger, voice, env
        self.handed = {}  # BOARD_FROM: the 看板 redrawn after a proposal decided on it (app/unified.py fills it).
        self.busy = threading.Lock()
        self.progress = {"done": 0, "total": 0}
        self.counted = (0.0, 0)

    # The thread ------------------------------------------------------------------------------------------------------

    def threads(self):
        return ((self.name, self.run),) if proposals.enabled() else ()

    def run(self, ctx) -> None:
        if self.runtime.shutdown.wait(START_WAIT):
            return
        every(self.runtime, TICK, self.tick, args=(ctx,), isolate=self.name)

    def chat(self) -> str:
        return self.runtime.access.owner_chat() or ""

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
        self.follow(ctx, chat)
        self.scheduled(ctx, chat, now)
        if not self.busy.locked():
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

        jobs = select_job_records(state, lambda record: ended(record) and recent(record), chat_id=chat, narrow=recent)
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
            why.append(TURN)  # Each turn the owner started: may it join a project now (``realtime``, once a 话题要点).
        once = [reason for reason in why if reason not in ("essence", TURN)]
        if once:
            proposals.change_topic(state, topic, lambda flags: flags.update(fired={**(flags.get("fired") or {}),
                                                                                    **{reason: now for reason in once}}))
        return why

    def topic_count(self, state, chat, now) -> int:
        """How many topics the chat has had (the butler's family left out), read once every COUNT_KEPT seconds."""
        at, count = self.counted
        if now - at < COUNT_KEPT and at:
            return count
        topics = {record.get("topic_id") for _, record in directory_store(jobs_dir(state)).query(chat_id=chat)}
        count = sum(1 for topic in topics if isinstance(topic, str) and topic and not is_butler_topic(topic, chat))
        self.counted = (now, count)
        return count

    def drain(self, ctx, chat, now) -> None:
        """The real-time 整理 the scan queued, each topic once."""
        state = Path(ctx.state_dir)
        taken = []
        proposals.change(state, lambda data: (taken.extend(data.get("queue") or []), data.update(queue=[])))
        done = set()
        for item in taken:
            if item.get("project"):
                try:
                    self.sweep(ctx, chat, str(item["project"]), now)
                except Exception as exc:  # noqa: BLE001 - the topics' own turns still bring them in
                    self.runtime.log(f"project tidy sweep failed project={str(item['project'])[:8]}: {type(exc).__name__}")
                continue
            topic = str(item.get("topic") or "")
            if topic in done or not projects.TOPIC_RE.fullmatch(topic):
                continue
            done.add(topic)
            try:
                self.realtime(ctx, chat, topic, str(item.get("reason") or ""), now)
            except Exception as exc:  # noqa: BLE001 - the next moment tries again
                self.runtime.log(f"project tidy realtime failed topic={topic[:12]}: {type(exc).__name__}")

    # Reading -----------------------------------------------------------------------------------------------------------

    def judge_tools(self):
        app = self.env.topics.apps.get("claude") if self.env is not None and self.env.topics is not None else None
        harness = getattr(app, "harness", None)
        return getattr(harness, "backend", None), getattr(harness, "context", None)

    def live_projects(self, state, chat) -> list:
        return [record for record in projects.records(state, chat) if record.get("state") in (projects.ACTIVE, projects.PAUSED)]

    def recent_topics(self, state, chat, now, *, scope="", records=()) -> list:
        """The topics a 整理 looks at, the latest first: those with a turn in the last days, and every topic of a live project."""
        window = now - proposals.number("days") * 86400
        members = {topic for record in records for topic in record.get("threads") or ()}
        latest = {}
        for _, record in directory_store(jobs_dir(state)).query(chat_id=chat):
            topic = record.get("topic_id")
            if not isinstance(topic, str) or not projects.TOPIC_RE.fullmatch(topic) or is_butler_topic(topic, chat):
                continue
            stamp = epoch_seconds(record.get("updated_at")) or 0
            latest[topic] = max(latest.get(topic, 0), stamp)
        chosen = [topic for topic, stamp in latest.items() if stamp >= window or topic in members]
        if scope:
            chosen = [topic for topic in chosen if self.in_scope(state, topic, scope, records)]
        return sorted(chosen, key=lambda topic: -latest[topic])[:sorter.MAX_TOPICS]

    @staticmethod
    def in_scope(state, topic, scope, records) -> bool:
        word = scope.lower()
        found = projects.topic_project(state, topic)
        if found is not None and any(record["id"] == found.id and (word in str(record.get("name") or "").lower() or record["id"] == scope)
                                     for record in records):
            return True
        text = " ".join([label_title(state, topic)[1], topic_gist.lines(topic_gist.read(state, topic))]).lower()
        return word in text
