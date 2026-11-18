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
  live project's); each turn the owner started (TURN) the judge reads this topic alone against the live projects when its gist
  changed (``turn_judge``); a project just made looks over the recent topics once (``sweep``).
  A join the judge is sure of (JOIN_SURE) is done at once (app/projects.py join_topic) and the topic's cards get 已归进「X」 · 移出;
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
written with their own records' sequence."""
from __future__ import annotations

import hashlib
import re
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

from covey.app import project_sorter as sorter
from covey.app import topic_links
from covey.app.card_lanes import after_ack
from covey.cards import project_proposal as cards
from covey.core import project_proposals as proposals
from covey.core import projects, topic_gist
from covey.core.butler import is_butler_topic
from covey.core.card_sequence import file_card, reserve
from covey.core.checkpoint import checkpoint_gist, read_checkpoint
from covey.core.jobs import job_key, jobs_dir, select_job_records, topic_job_records
from covey.core.locks import resource_lock
from covey.core.loops import every
from covey.core.state import claim_once, epoch_seconds, read_object, write_json
from covey.core.statuses import RUNNING
from covey.core.store import directory_store
from covey.core.topics import label_title, topic_file

TICK = 20
START_WAIT = 30
PROGRESS_GAP = 3.0  # Seconds between two redraws of the card while it reads.
COUNT_KEPT = 600.0  # The chat's topic count is read again after this.
QUEUE_MAX = proposals.QUEUE_MAX
TURN = "turn"  # The reason a turn the owner started gives (``saw``): a join only, and only when the 话题要点 changed.
BOARD_FROM = "board"  # A proposal's 建专项 / 暂不考虑 clicked on the 看板 (cards/board.py).
CHOICES = 20  # Topics the proposal card's select offers: the proposal's, then the recent others.
NOW_CHARS = 80  # Of a topic's latest conclusion on the proposal card (现在：…).
INBOUND = "inbound-messages"
BUSY_PREFIX, STARTED_WORDS = proposals.BUSY_PREFIX, proposals.STARTED_WORDS
BUSY_WORDS = BUSY_PREFIX + "（{done}/{total}）"
FEW_WORDS = "话题还不多（不到 {} 个），先不整理专项。"
OFF_WORDS = "整理专项没有开（COVEY_PROJECT_TIDY）。"


def ended(record) -> bool:
    return record.get("status") not in RUNNING and bool(record.get("status"))


class ProjectTidy:
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

    # Real time -----------------------------------------------------------------------------------------------------------

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
            if pid and self.join(ctx, self.rule_join(topic, pid, why), chat, by="rule"):
                self.runtime.log(f"project tidy realtime topic={topic[:12]} reason={reason} outcome=joined by=rule")
                return "joined"
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
        """A project just made (app/projects.py queue_sweep) takes in the recent topics that belong to it: by the rule, then one
        judge call over the rest against this project alone, the joins it is sure of. How many joined."""
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
            found, why = self.by_rule(state, chat, topic, records)
            if found == pid and self.join(ctx, self.rule_join(topic, pid, why), chat, by="rule"):
                joined += 1
            elif not found:
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
            note = f"又发现 {self.labels(state, more)} 也是这件事，补进来了。"
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

    # Doing -------------------------------------------------------------------------------------------------------------

    def create(self, ctx, chat, record, *, owner_named, name="", goal="") -> str:
        projects_ = getattr(self.env, "projects", None)
        if projects_ is None:
            return ""
        return projects_.gather(chat=chat, name=name or record.get("name"), goal=goal or record.get("goal") or "",
                                topics=record.get("topics") or (), key=record["id"], why=record.get("why") or "",
                                owner_named=owner_named)

    def join(self, ctx, made, chat, *, by="tidy") -> bool:
        """A sure join done (app/projects.py join_topic; ``by`` "rule" when the 话题要点 decided it) and the topic's cards marked;
        whether it was."""
        from covey.app.projects import join_topic

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
        from covey.app.projects import leave_topic

        state = Path(ctx.state_dir)
        name = projects.read_record(state, pid).get("name") or "专项"
        if not leave_topic(state, pid, topic, why=why):
            return f"这个话题已经不在专项「{name}」里了。"
        proposals.change_topic(state, topic, lambda flags: flags.update(never_auto=True, left=[*(flags.get("left") or []),
                                                                                              {"project": pid, "at": time.time()}]))
        self.mark(ctx, chat, topic, pid, "")
        return f"已把这个话题移出专项「{name}」，以后它不会再被自动归进专项。"

    def apply(self, ctx, chat, record) -> tuple:
        """Do an accepted proposal: (ok, the line said)."""
        from covey.app.projects import join_topic, leave_topic

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
            for other in record["projects"][1:]:
                gone = projects.read_record(state, other)
                if not gone or gone.get("state") not in (projects.ACTIVE, projects.PAUSED):
                    continue
                for topic in list(gone.get("threads") or ()):
                    leave_topic(state, other, topic, why="合并进另一个专项")
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
            if projects.active_count(state) >= projects.MAX_ACTIVE:
                return False, f"已经有 {projects.MAX_ACTIVE} 个专项在做，没拆"
            for topic in record["topics"]:
                leave_topic(state, pid, topic, why="拆出去另建专项")
            made = self.create(ctx, chat, record, owner_named=False)
            return (True, f"已拆出专项「{record.get('name')}」") if made else (False, "没拆成")
        return False, "不认识的建议"

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
            chat = self.chat()
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

    # Scheduled and asked for ---------------------------------------------------------------------------------------

    def scheduled(self, ctx, chat, now) -> None:
        """整理 at the 早晚汇总's times (COVEY_DIGEST_TIMES), each time once (its own ``last_slot``)."""
        from covey.app.digest import due, parse_times
        from covey.core import env as cenv

        times = parse_times(cenv.get("COVEY_DIGEST_TIMES", "").strip())
        if not times:
            return
        slot = due(times, now, proposals.load(ctx.state_dir).get("last_slot"))
        if slot is None or self.busy.locked():
            return
        proposals.change(ctx.state_dir, lambda data: data.update(last_slot=slot))
        self.start(ctx, origin="scheduled", wait=True)

    def start(self, ctx=None, *, origin="asked", scope="", wait=False) -> str:
        """A 整理 of every project and the recent topics (``scope``: only what has to do with that word, or that project), in a
        thread of its own (``wait``: in this one); the words for whoever asked."""
        if not proposals.enabled():
            return OFF_WORDS
        ctx = ctx if ctx is not None else SimpleNamespace(state_dir=Path(self.env.state_dir), profile=self.env.profile)
        chat = self.chat()
        if not chat:
            return "还不知道你的私聊，先在飞书里和机器人说句话"
        if not self.busy.acquire(blocking=False):
            return BUSY_WORDS.format(**self.progress)
        state = Path(ctx.state_dir)
        if self.topic_count(state, chat, time.time()) < proposals.number("min_topics"):
            self.busy.release()
            return FEW_WORDS.format(proposals.number("min_topics"))
        self.progress = {"done": 0, "total": 0}
        run = time.strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:6]
        if wait:
            self.guarded(ctx, chat, run, origin, scope)
        else:
            threading.Thread(target=self.guarded, args=(ctx, chat, run, origin, scope), name="project-tidy-run", daemon=True).start()
        return STARTED_WORDS

    def guarded(self, ctx, chat, run, origin, scope) -> None:
        """``whole`` holding ``busy`` (taken by ``start``), released however it ends."""
        try:
            self.whole(ctx, chat, run, origin, scope)
        finally:
            self.busy.release()

    def run_path(self, state, run) -> Path:
        return proposals.folder(state) / "runs" / f"{run}.json"

    def whole(self, ctx, chat, run, origin, scope) -> dict:
        """One whole 整理 (``guarded`` holds ``busy``): the card, the backfill, the judge, what is done, the card again."""
        state = Path(ctx.state_dir)
        view = {"run": run, "state": "running", "progress": {}, "scope": scope, "did": [], "rows": [], "skipped": []}
        record = {"run": run, "chat_id": chat, "origin": origin, "scope": scope, "started_at": time.time(), "view": view}
        try:
            record["message_id"] = self.send_tidy(ctx, chat, run, view)
            self.save_run(state, record)
            self.work(ctx, chat, record)
        except Exception as exc:  # noqa: BLE001 - said on the card
            self.runtime.log(f"project tidy run failed: {type(exc).__name__}: {str(exc)[:200]}")
            record["view"] = {**record["view"], "state": "failed", "why": "出错了"}
        finally:
            record["finished_at"] = time.time()
            self.save_run(state, record)
            self.redraw(ctx, record)
            proposals.change(state, lambda data: data.update(last_run={"run": run, "at": time.time(), "origin": origin}))
        return record

    def work(self, ctx, chat, record) -> None:
        state, view, now = Path(ctx.state_dir), record["view"], time.time()
        records = self.live_projects(state, chat)
        if record.get("scope") and projects.valid_id(record["scope"]):
            records = [item for item in records if item["id"] == record["scope"]] or records
        topics = self.recent_topics(state, chat, now, scope=record.get("scope") or "", records=records)
        hidden = [topic for topic in topics if sorter.hidden(state, topic)]
        topics = [topic for topic in topics if topic not in hidden]
        backend, context = self.judge_tools()
        missing = [topic for topic in topics if sorter.needs_gist(state, topic, sorter.material(state, chat, topic, now).last_job)]
        self.progress = {"done": 0, "total": len(missing)}
        drawn, unreadable = 0.0, []
        for number, topic in enumerate(missing, 1):
            label = label_title(state, topic)[0] or "话题"
            self.progress = {"done": number, "total": len(missing)}
            view["progress"] = {"done": number, "total": len(missing), "current": label}
            if time.monotonic() - drawn >= PROGRESS_GAP:
                self.redraw(ctx, record)
                drawn = time.monotonic()
            reason = sorter.backfill(state, chat, topic, backend=backend, ctx=context, log=self.runtime.log, now=now)
            if reason:
                unreadable.append((topic, reason))
        topics = [topic for topic in topics if topic not in {topic for topic, _ in unreadable}]
        # Which ones and why (the owner, 2026-11-18: “读不了”的话题，要写清是哪几个、为什么读不了).
        if unreadable:
            view["skipped"].append(f"{len(unreadable)} 个话题读不了，没参与这次整理：" + "；".join(
                f"{self.linked(ctx, [topic])}（{UNREADABLE_WORDS.get(reason, '读的时候出错')}）" for topic, reason in unreadable))
            view["skipped_md"] = True
        if hidden:
            view["skipped"].append(f"{len(hidden)} 个话题开了脱敏，内容不外发，没参与这次整理：" + self.linked(ctx, hidden))
            view["skipped_md"] = True
        view["progress"] = {"done": len(missing), "total": len(topics), "current": ""}
        self.redraw(ctx, record)
        items = [sorter.material(state, chat, topic, now) for topic in topics]
        verdict = sorter.judge(state, records, items, backend=backend, ctx=context, scope=record.get("scope") or "",
                               lessons=proposals.lessons(state), run=record["run"], log=self.runtime.log)
        if verdict.why not in ("ok", "no_topics"):
            view.update(state="failed", why=FAILED_WORDS.get(verdict.why, "判断没有结果"))
            return
        self.settle(ctx, chat, record, verdict.suggestions)
        self.expire_alone(ctx)
        view["state"] = "done"

    def settle(self, ctx, chat, record, suggestions) -> None:
        """What the judge said: the sure joins and the folds done at once, the rest proposals on the card (at most CARD_ROWS)."""
        state, view = Path(ctx.state_dir), record["view"]
        for made in suggestions:
            kind = made["kind"]
            if kind == proposals.JOIN and made["confidence"] >= proposals.JOIN_SURE and not proposals.never_auto(state, made["topics"][0]):
                if self.join(ctx, made, chat):
                    view["did"].append(f"已把 {self.labels(state, made['topics'])} 归进「{self.project_name(state, made['project'])}」")
                continue
            if kind == proposals.FOLD:
                if not self.foldable(state, made["project"]):
                    continue
                name = self.project_name(state, made["project"])
                self.fold(state, made["project"])
                proposals.put_proposal(state, {**made, "state": proposals.DONE, "decided_at": time.time()})
                view["did"].append(f"已收起「{name}」：{made.get('why') or '最近没有要做的事'}，有新的事会自动展开")
                continue
            if kind == proposals.JOIN:
                continue  # Not sure enough to do, not worth asking: the next 整理 looks again.
            if len(view["rows"]) >= cards.CARD_ROWS:
                break
            if proposals.declined_like(state, kind, made["topics"], made.get("project") or "", made.get("projects") or ()):
                continue
            if kind == proposals.CREATE:
                # Each new project its own proposal card, the same as a real-time one (the owner, 2026-11-18: 每个话题都要能点开，
                # 附摘要；不准用“等”；能自己增删话题、改名再确认): the 整理专项 card only names it.
                if self.propose(ctx, chat, {**made, "run": record["run"]}) == "proposed":
                    view.setdefault("proposed", []).append(str(made.get("name") or ""))
                else:
                    # The same proposal already waits for him (the owner, 2026-11-18 23:19: 为啥现在我点了整理专项说不需要呢 —
                    # it was dropped as nothing new): named as still waiting, to decide on the 看板.
                    found = proposals.read_proposal(state, made["id"])
                    if found.get("state") != proposals.OPEN or not (found.get("card") or {}).get("message_id"):
                        found = proposals.overlapping(state, proposals.free(state, made["topics"]))  # Merged into it.
                    name = str(found.get("name") or made.get("name") or "")
                    if found and name not in view.get("pending", ()):
                        view.setdefault("pending", []).append(name)
                continue
            saved, _ = proposals.put_proposal(state, made)
            if saved.get("state") != proposals.OPEN or any(row["proposal"] == saved["id"] for row in view["rows"]):
                continue
            proposals.update_proposal(state, saved["id"], lambda data: data.update(run=record["run"]))
            view["rows"].append({"proposal": saved["id"], "line": self.line(state, saved, ctx), "md": True, "state": proposals.OPEN})

    def expire_alone(self, ctx) -> int:
        """The open proposals of one topic, made before a project needed two (app/project_sorter.py MIN_CREATE): 已过时,
        their cards redrawn so; how many."""
        state, gone = Path(ctx.state_dir), 0
        for record in proposals.proposals(state, [proposals.OPEN]):
            if record.get("kind") != proposals.CREATE or len(set(record.get("topics") or ())) >= sorter.MIN_CREATE:
                continue
            view = {**(record.get("view") or self.proposal_view(state, record)), "state": proposals.EXPIRED,
                    "note": "已过时：专项要两个以上话题，这条不再提。", "lesson": False}
            proposals.update_proposal(state, record["id"], lambda data: data.update(state=proposals.EXPIRED,
                                                                                      decided_at=time.time(), view=view))
            self.patch_own(ctx, str((record.get("card") or {}).get("message_id") or ""), cards.proposal_card(view))
            gone += 1
        return gone

    # Proposals that follow the joins ------------------------------------------------------------------------------------

    def follow(self, ctx, chat) -> int:
        """The open proposals some of whose topics are now in a project, however they got there (建专项, 自动收, the lead's
        加话, a merge, by hand): a 新建 one keeps the rest when MIN_CREATE are left (its card redrawn with them), else it ends
        (已过时, the card says where they went); a 归进 one ends. Each tick; how many changed. Rules 1 and 3: each record is
        changed under its own lock, its card written after."""
        state, changed = Path(ctx.state_dir), 0
        for record in proposals.proposals(state, [proposals.OPEN]):
            kind, topics = record.get("kind"), list(dict.fromkeys(record.get("topics") or ()))
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
                    rest = (f"；剩下的 {self.labels(state, left)} 一个话题成不了专项" if left else "") if kind == proposals.CREATE else ""
                    done = self.retire(ctx, record, f"{went}{rest}，这条不再提。")
            except Exception as exc:  # noqa: BLE001 - the next tick tries again
                self.runtime.log(f"project tidy follow failed proposal={record['id'][:8]}: {type(exc).__name__}")
                continue
            if done:
                changed += 1
                self.runtime.log(f"project tidy proposal followed proposal={record['id'][:8]} left={len(left)}")
        return changed + self.unrepeat(ctx)

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
        return "；".join(f"{self.labels(state, group)} 已归进专项「{self.project_name(state, pid)}」" if pid
                        else f"{self.labels(state, group)} 已归进专项" for pid, group in by.items())

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
            self.patch_own(ctx, str((record.get("card") or {}).get("message_id") or ""), cards.proposal_card(view))
        return bool(done)

    def foldable(self, state, pid) -> bool:
        from covey.core.jobs import topic_job_records

        record = projects.read_record(state, pid)
        if record.get("state") != projects.ACTIVE:
            return False
        topics = [record.get("lead_topic_id"), *(record.get("threads") or ())]
        return not any(job.get("status") in RUNNING or job.get("status") == "waiting_user"
                       for topic in topics if topic for job in topic_job_records(state, topic))

    @staticmethod
    def project_name(state, pid) -> str:
        return str(projects.read_record(state, pid).get("name") or "专项")

    @staticmethod
    def labels(state, topics) -> str:
        """Every topic's T number (the owner, 2026-11-18: 不准用“等”字省略), plain: the log's and a proposal's text."""
        return "、".join(label_title(state, topic)[0] or "话题" for topic in topics)

    def linked(self, ctx, topics) -> str:
        """Every topic as Markdown, its T number a link that opens it and its title after it."""
        state, words = Path(ctx.state_dir), []
        for topic in topics:
            label, title = label_title(state, topic)
            if not label:  # A topic without a number (an old one, or a Harness's own): what the owner said first names it.
                said = sorter.material(state, self.chat() or "", topic, time.time()).owner
                label, title = "未编号话题", title or (sorter._cut(said[0], 24) if said else "")
            try:
                link = self.link(state, topic, profile=getattr(ctx, "profile", None))
            except Exception:  # noqa: BLE001 - a T number without its link
                link = ""
            shown = f"[{label}]({link})" if link else label
            words.append(f"{shown} {cards.literal_text(title)}" if title else shown)
        return "、".join(words)

    def line(self, state, record, ctx=None) -> str:
        """One row of the 整理专项 card: Markdown with every topic linked when ``ctx`` is given (a row marked ``md``), else plain
        words (the log's, and rows kept before)."""
        kind, why = record["kind"], record.get("why") or ""
        esc = cards.literal_text if ctx is not None else (lambda text: text)
        topics = self.linked(ctx, record.get("topics") or ()) if ctx is not None else self.labels(state, record.get("topics") or ())
        name = esc(self.project_name(state, record.get("project"))) if record.get("project") else ""
        said = {
            proposals.CREATE: f"新建「{esc(record.get('name') or '')}」：{topics}",
            proposals.LEAVE: f"把 {topics} 移出「{name}」",
            proposals.MERGE: "合并" + "、".join(f"「{esc(self.project_name(state, pid))}」" for pid in record.get("projects") or ())
                             + (f"，叫「{esc(record['name'])}」" if record.get("name") else ""),
            proposals.SPLIT: f"从「{name}」拆出 {topics}，另建「{esc(record.get('name') or '')}」",
            proposals.RENAME: f"「{name}」改名「{esc(record.get('name') or '')}」",
            proposals.JOIN: f"把 {topics} 归进「{name}」",
            proposals.FOLD: f"收起「{name}」"}.get(kind, kind)
        return said + (f"（{esc(why)}）" if why else "")

    def send_tidy(self, ctx, chat, run, view) -> str:
        sent = []
        self.messenger.send_message(ctx.profile, chat, "整理专项：正在整理", self.voice.safe_key(self.voice.key("tidy-run", run)),
                                    voice=self.voice, card=cards.tidy_card(view), on_card=sent.append)
        return sent[0] if sent else ""

    def save_run(self, state, record) -> None:
        path = self.run_path(state, record["run"])
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(path, record, ensure_ascii=False, indent=1)

    def redraw(self, ctx, record) -> None:
        if record.get("message_id"):
            self.messenger.patch_card(ctx.profile, record["message_id"], cards.tidy_card(record["view"]))

    # The clicks ----------------------------------------------------------------------------------------------------

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
        chat = str(event.get("chat_id") or "") or self.chat()
        if kind == cards.START_KIND:
            words = self.start(ctx, origin="settings")
            after_ack(lambda: self.messenger.send_message(ctx.profile, chat, words, self.voice.safe_key(
                self.voice.key("tidy-start", event_id)), voice=self.voice))
            return "tidy_start"
        if kind == cards.LEAVE_KIND:
            topic, pid = str(raw.get("topic_id") or ""), str(raw.get("project_id") or "")
            if not projects.TOPIC_RE.fullmatch(topic) or not projects.valid_id(pid):
                return "tidy_bad_value"

            def leave():
                words = self.leave(ctx, chat, topic, pid)
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

    def lesson(self, ctx, event, raw, message) -> str:
        from covey.app.card_actions import form_fields

        state = Path(ctx.state_dir)
        text = " ".join(str((form_fields(event) or {}).get(cards.WHY_FIELD) or "").split())
        if text:
            proposals.add_lesson(state, text, kind="leave" if raw.get("topic") else "decline")
        if raw.get("proposal"):
            record = proposals.read_proposal(state, str(raw["proposal"]))
            view = {**(record.get("view") or self.proposal_view(state, record)), "lesson": False,
                    "note": str((record.get("view") or {}).get("note") or "") + ("（理由已记下）" if text else "")}
            proposals.update_proposal(state, record["id"], lambda data: data.update(view=view))
            after_ack(lambda: self.patch_own(ctx, message, cards.proposal_card(view)))
        else:
            after_ack(lambda: self.patch_own(ctx, message, cards.lesson_card(
                "理由已记下，整理专项以后照这个来。" if text else "好的，没记理由。")))
        return "tidy_lesson"

    def proposal_click(self, ctx, chat, event, raw, message) -> str:
        """A click on the proposal card, or on its block on the 看板 (``from`` BOARD_FROM): the 看板 then redrawn after
        whatever the click does (its follow-ups run in order in this lane)."""
        outcome = self.decide(ctx, chat, event, raw, message)
        hand = self.handed.get(BOARD_FROM)
        if raw.get("from") == BOARD_FROM and hand is not None and message and outcome != "tidy_stale":
            after_ack(lambda: hand(ctx, message))
        return outcome

    def decide(self, ctx, chat, event, raw, message) -> str:
        """A click on the proposal card."""
        from covey.app.card_actions import form_fields

        state, kind = Path(ctx.state_dir), raw["kind"]
        record = proposals.read_proposal(state, str(raw.get("proposal") or ""))
        # 建专项 and 暂不考虑 on the 看板 (cards/board.py, ``from`` "board"; the owner, 2026-11-18: 还在等主人定的提议卡要在看板上
        # 直接能点开处理): the proposal's own card is the one redrawn, then the 看板 (``handed``).
        board = raw.get("from") == BOARD_FROM and kind in (cards.CREATE_KIND, cards.DECLINE_KIND)
        own = str((record or {}).get("card", {}).get("message_id") or "") if record else ""
        if not record or (not board and own not in ("", message)):
            return "tidy_stale"
        board_message, message = (message, own) if board else ("", message)
        view = dict(record.get("view") or self.proposal_view(state, record))
        if kind == cards.AUTO_KIND:
            proposals.set_pref(state, auto_create=True)
            view["ask_auto"] = False
            proposals.update_proposal(state, record["id"], lambda data: data.update(view=view))
            after_ack(lambda: self.patch_own(ctx, message, cards.proposal_card(view)))
            return "tidy_auto"
        if record.get("state") != proposals.OPEN:
            after_ack(lambda: self.patch_own(ctx, message, cards.proposal_card({**view, "state": record["state"]})))
            return "tidy_decided"
        if kind == cards.RENAME_KIND:
            after_ack(lambda: self.patch_own(ctx, message, cards.rename_form(view)))
            return "tidy_rename"
        if kind == cards.BACK_KIND:
            after_ack(lambda: self.patch_own(ctx, message, cards.proposal_card(view)))
            return "tidy_back"
        if kind in (cards.DECLINE_KIND, cards.NEVER_KIND):
            never = kind == cards.NEVER_KIND
            proposals.update_proposal(state, record["id"], lambda data: data.update(
                state=proposals.NEVER if never else proposals.DECLINED, decided_at=time.time()))
            proposals.set_pref(state, accepted_in_row=0, **({"no_proposals": True} if never else {}))
            view.update(state=proposals.NEVER if never else proposals.DECLINED, lesson=True,
                        note="以后不再提议建专项；要开专项，跟管家说“开个专项”。" if never else
                        "暂不考虑：这几个话题不会再这样提，等又多出相关的话题才会再提。")
            proposals.update_proposal(state, record["id"], lambda data: data.update(view=view))
            after_ack(lambda: self.patch_own(ctx, message, cards.proposal_card(view)))
            return "tidy_never" if never else "tidy_decline"
        # 建专项, or the rename form's 建专项.
        fields = form_fields(event) or {}
        name = proposals.clean_name(fields.get(cards.NAME_FIELD)) if kind == cards.NAMED_KIND else record.get("name")
        goal = (" ".join(str(fields.get(cards.GOAL_FIELD) or "").split())[:200] if cards.GOAL_FIELD in fields
                else record.get("goal"))
        if kind == cards.NAMED_KIND and not name:
            return "tidy_no_name"
        named = name != record.get("name")  # The owner's own name: 整理专项 never renames it, and 以后直接建 is not offered for it.
        topics, unknown = self.chosen(state, chat, view, fields)
        if topics is None:
            topics = list(record.get("topics") or ())
        if not topics:
            after_ack(lambda: self.messenger.send_message(ctx.profile, chat, "没选话题，专项没建：在卡上勾上话题或填 T 号再点「建专项」。",
                                                          self.voice.safe_key(self.voice.key("tidy-none", str(event.get("event_id") or ""))),
                                                          voice=self.voice))
            return "tidy_no_topics"
        record = {**record, "topics": topics}
        proposals.update_proposal(state, record["id"], lambda data: data.update(state=proposals.ACCEPTED, decided_at=time.time(),
                                                                                  name=name, goal=goal or "", topics=topics))

        def build():
            if projects.active_count(state) >= projects.MAX_ACTIVE:
                pid, words = "", f"已经有 {projects.MAX_ACTIVE} 个专项在做，没建：先收尾或停掉一个，再说“整理一下专项”"
            else:
                pid = self.create(ctx, chat, {**record, "name": name, "goal": goal}, owner_named=named, name=name, goal=goal)
                words = f"已建专项「{name}」：专项卡在聊天区，负责人接着跟进。" if pid else "专项没建成，过一会儿再说“整理一下专项”。"
                if pid and unknown:
                    words += f"（{'、'.join(unknown)} 不是你最近的话题，没加）"
            proposals.update_proposal(state, record["id"], lambda data: data.update(
                state=proposals.DONE if pid else proposals.FAILED, project_made=pid))

            def count(data):
                prefs = data.get("prefs") or {}
                data["prefs"] = {**prefs, "accepted_in_row": 0 if named or not pid else int(prefs.get("accepted_in_row") or 0) + 1}
            proposals.change(state, count)
            rows = self.topic_rows(ctx, chat, topics) if view.get("rows") else view.get("rows")
            shown = {**view, "name": name, "goal": goal, "state": proposals.DONE if pid else proposals.FAILED, "note": words,
                     "ask_auto": False, "rows": rows, "labels": [label_title(state, topic) for topic in topics]}
            proposals.update_proposal(state, record["id"], lambda data: data.update(view=shown))
            self.patch_own(ctx, message, cards.proposal_card(shown))
        after_ack(build)
        return "tidy_named" if named else "tidy_create"

    def patch_own(self, ctx, message, card):
        """A proposal's own card redrawn; none when it has no card (a row of an older 整理专项 card, clicked on the 看板)."""
        return self.messenger.patch_card(ctx.profile, message, card) if message else False

    def row_click(self, ctx, chat, event, raw, message) -> str:
        """好, 不用 and 全部照办 on the 整理专项 card."""
        state, run = Path(ctx.state_dir), str(raw.get("run") or "")
        if not run.replace("-", "").isalnum():
            return "tidy_bad_value"
        path = self.run_path(state, run)
        record = read_object(path)
        if not record or record.get("message_id") != message:
            return "tidy_stale"
        if self.busy.locked() and (record.get("view") or {}).get("state") == "running":
            return "tidy_running"
        rows = [row for row in (record.get("view") or {}).get("rows") or ()]
        chosen = [row for row in rows if row.get("state") == proposals.OPEN and (raw["kind"] == cards.ALL_KIND
                                                                              or row.get("proposal") == raw.get("proposal"))]
        if not chosen:
            return "tidy_decided"

        def act():
            for row in chosen:
                proposal = proposals.read_proposal(state, row["proposal"])
                if not proposal or proposal.get("state") != proposals.OPEN:
                    row.update(state=proposal.get("state") or proposals.EXPIRED)
                    continue
                if raw["kind"] == cards.DECLINE_KIND:
                    proposals.update_proposal(state, proposal["id"], lambda data: data.update(state=proposals.DECLINED,
                                                                                              decided_at=time.time()))
                    row.update(state=proposals.DECLINED, note="不用")
                    continue
                proposals.update_proposal(state, proposal["id"], lambda data: data.update(state=proposals.ACCEPTED,
                                                                                          decided_at=time.time()))
                try:
                    ok, words = self.apply(ctx, chat, proposal)
                except Exception as exc:  # noqa: BLE001 - this row only
                    ok, words = False, f"出错了（{type(exc).__name__}）"
                proposals.update_proposal(state, proposal["id"], lambda data: data.update(
                    state=proposals.DONE if ok else proposals.FAILED, note=words))
                row.update(state=proposals.DONE if ok else proposals.FAILED, note=words)
            saved = read_object(path)
            saved["view"] = {**saved.get("view", {}), "rows": rows}
            write_json(path, saved, ensure_ascii=False, indent=1)
            self.messenger.patch_card(ctx.profile, message, cards.tidy_card(saved["view"]))
        after_ack(act)
        return {cards.OK_KIND: "tidy_ok", cards.ALL_KIND: "tidy_all"}.get(raw["kind"], "tidy_row_decline")


def gist_sign(state, topic) -> str:
    """What the judge reads of a topic's 话题要点 that may change its project, as a short hash ('' without one)."""
    gist = topic_gist.gist_of(topic_gist.read(state, topic))
    if not gist:
        return ""
    text = "\n".join([str(gist.get("essence") or ""), str(gist.get("goal") or ""), ",".join(map(str, gist.get("related") or ()))])
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def cut(text) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= NOW_CHARS else text[:NOW_CHARS - 1] + "…"


def where_now(state, chat, topic) -> str:
    """Where ``topic`` is now, in its own words: the phase its newest turn's checkpoint says (a short phrase in the owner's
    language, what the task card's status line shows), or ''."""
    jobs = sorted(topic_job_records(Path(state), topic, chat_id=chat), key=lambda job: int(job.get("job_id") or 0))
    for job in reversed(jobs[-3:]):
        path = str(job.get("checkpoint_path") or "")
        phase = read_checkpoint(Path(path)).get("phase") if path else ""
        if isinstance(phase, str) and phase.strip():
            return cut(phase)
    return ""


def latest(conclusions) -> str:
    """The first sentence of a topic's latest conclusion when it has no phase to say: where it is now, on the proposal card
    ('' for one too short to say anything, as a lone "!")."""
    text = " ".join(str(conclusions[-1] if conclusions else "").split())
    first = re.split(r"(?<=[。！？!?])", text, maxsplit=1)[0] if text else ""
    return cut(first) if len(first) >= 6 else ""


UNREADABLE_WORDS = {"model_error": "模型调用出错", "refused": "官方内容安全拦了", "too_long": "内容太长，一次读不完",
                    "exit_nonzero": "读它的 Claude Code 出错退出了", "timeout": "读超时了",
                    "bad_output": "模型的回答看不懂", "empty": "里面还没有你说的话", "redacted": "开了脱敏，内容不外发",
                    "no_backend": "没有可用的 Claude Code 来读", "error": "读的时候出错"}
FAILED_WORDS = {"timeout": "判断超时了", "no_backend": "没有可用的 Claude Code 来判断", "bad_output": "判断的回答看不懂",
                "model_error": "模型拒绝了这次判断", "exit_nonzero": "判断的进程出错了", "spawn_failed": "判断的进程没起来"}
