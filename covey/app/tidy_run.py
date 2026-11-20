"""A whole 整理 (scheduled or asked for) and the 整理专项 card it draws: the run's record, its progress, the judge's suggestions as
rows (docs/butler.md 整理专项). Joins and folds through app/tidy_apply.py, proposals through app/tidy_proposals.py, the rows' words
from app/tidy_words.py; the clicks on its card are app/tidy_clicks.py's, above it."""
from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

from covey.app import project_sorter as sorter
from covey.app import tidy_facts as facts
from covey.cards import project_proposal as cards
from covey.core import project_proposals as proposals
from covey.core import projects
from covey.core.state import write_json
from covey.core.topics import label_title

PROGRESS_GAP = 3.0  # Seconds between two redraws of the card while it reads.
BUSY_WORDS = proposals.BUSY_PREFIX + "（{done}/{total}）"
FEW_WORDS = "话题还不多（不到 {} 个），先不整理专项。"
OFF_WORDS = "整理专项没有开（COVEY_PROJECT_TIDY）。"
UNREADABLE_WORDS = {"model_error": "模型调用出错", "refused": "官方内容安全拦了", "too_long": "内容太长，一次读不完",
                    "exit_nonzero": "读它的 Claude Code 出错退出了", "timeout": "读超时了",
                    "bad_output": "模型的回答看不懂", "empty": "里面还没有你说的话", "redacted": "开了脱敏，内容不外发",
                    "no_backend": "没有可用的 Claude Code 来读", "error": "读的时候出错"}
FAILED_WORDS = {"timeout": "判断超时了", "no_backend": "没有可用的 Claude Code 来判断", "bad_output": "判断的回答看不懂",
                "model_error": "模型拒绝了这次判断", "exit_nonzero": "判断的进程出错了", "spawn_failed": "判断的进程没起来"}


class Runs:
    """The whole 整理. ``runtime``: the log and the owner's chat. ``messenger``, ``voice``: the 整理专项 card. ``env``: the judge's
    tools, the state directory. ``shared``: app/tidy_state.py TidyState (``busy``, ``progress``, ``topic_count``). ``words``:
    app/tidy_words.py TopicWords. ``applier``: app/tidy_apply.py Applier. ``proposal_cards``: app/tidy_proposals.py ProposalCards."""

    def __init__(self, runtime, messenger, voice, env, shared, words, applier, proposal_cards) -> None:
        self.runtime, self.messenger, self.voice, self.env = runtime, messenger, voice, env
        self.shared, self.words, self.applier, self.proposal_cards = shared, words, applier, proposal_cards

    def scheduled(self, ctx, chat, now) -> None:
        """整理 at the 早晚汇总's times (COVEY_DIGEST_TIMES), each time once (its own ``last_slot``)."""
        from covey.app.digest import due, parse_times
        from covey.core import env as cenv

        times = parse_times(cenv.get("COVEY_DIGEST_TIMES", "").strip())
        if not times:
            return
        slot = due(times, now, proposals.load(ctx.state_dir).get("last_slot"))
        if slot is None or self.shared.busy.locked():
            return
        proposals.change(ctx.state_dir, lambda data: data.update(last_slot=slot))
        self.start(ctx, origin="scheduled", wait=True)

    def start(self, ctx=None, *, origin="asked", scope="", wait=False) -> str:
        """A 整理 of every project and the recent topics (``scope``: only what has to do with that word, or that project), in a
        thread of its own (``wait``: in this one); the words for whoever asked."""
        if not proposals.enabled():
            return OFF_WORDS
        ctx = ctx if ctx is not None else SimpleNamespace(state_dir=Path(self.env.state_dir), profile=self.env.profile)
        chat = facts.owner_chat(self.runtime)
        if not chat:
            return "还不知道你的私聊，先在飞书里和机器人说句话"
        if not self.shared.busy.acquire(blocking=False):
            return BUSY_WORDS.format(**self.shared.progress)
        state = Path(ctx.state_dir)
        if self.shared.topic_count(state, chat, time.time()) < proposals.number("min_topics"):
            self.shared.busy.release()
            return FEW_WORDS.format(proposals.number("min_topics"))
        self.shared.progress = {"done": 0, "total": 0}
        run = time.strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:6]
        if wait:
            self.guarded(ctx, chat, run, origin, scope)
        else:
            threading.Thread(target=self.guarded, args=(ctx, chat, run, origin, scope), name="project-tidy-run", daemon=True).start()
        return proposals.STARTED_WORDS

    def guarded(self, ctx, chat, run, origin, scope) -> None:
        """``whole`` holding ``busy`` (taken by ``start``), released however it ends."""
        try:
            self.whole(ctx, chat, run, origin, scope)
        finally:
            self.shared.busy.release()

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
        records = facts.live_projects(state, chat)
        if record.get("scope") and projects.valid_id(record["scope"]):
            records = [item for item in records if item["id"] == record["scope"]] or records
        topics = facts.recent_topics(state, chat, now, scope=record.get("scope") or "", records=records)
        hidden = [topic for topic in topics if sorter.hidden(state, topic)]
        topics = [topic for topic in topics if topic not in hidden]
        backend, context = facts.judge_tools(self.env)
        missing = [topic for topic in topics if sorter.needs_gist(state, topic, sorter.material(state, chat, topic, now).last_job)]
        self.shared.progress = {"done": 0, "total": len(missing)}
        drawn, unreadable = 0.0, []
        for number, topic in enumerate(missing, 1):
            label = label_title(state, topic)[0] or "话题"
            self.shared.progress = {"done": number, "total": len(missing)}
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
                f"{self.words.linked(ctx, [topic])}（{UNREADABLE_WORDS.get(reason, '读的时候出错')}）" for topic, reason in unreadable))
            view["skipped_md"] = True
        if hidden:
            view["skipped"].append(f"{len(hidden)} 个话题开了脱敏，内容不外发，没参与这次整理：" + self.words.linked(ctx, hidden))
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
                if self.applier.join(ctx, made, chat):
                    view["did"].append(f"已把 {facts.labels(state, made['topics'])} 归进「{facts.project_name(state, made['project'])}」")
                continue
            if kind == proposals.FOLD:
                if not facts.foldable(state, made["project"]):
                    continue
                name = facts.project_name(state, made["project"])
                self.applier.fold(state, made["project"])
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
                if self.proposal_cards.propose(ctx, chat, {**made, "run": record["run"]}) == "proposed":
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
            view["rows"].append({"proposal": saved["id"], "line": self.words.line(state, saved, ctx), "md": True, "state": proposals.OPEN,
                                 "item": self.words.row_item(ctx, saved)})

    def expire_alone(self, ctx) -> int:
        """The open proposals of one topic, made before a project needed two (app/project_sorter.py MIN_CREATE): 已过时,
        their cards redrawn so; how many."""
        state, gone = Path(ctx.state_dir), 0
        for record in proposals.proposals(state, [proposals.OPEN]):
            if record.get("kind") != proposals.CREATE or len(set(record.get("topics") or ())) >= sorter.MIN_CREATE:
                continue
            view = {**(record.get("view") or facts.proposal_view(state, record)), "state": proposals.EXPIRED,
                    "note": "已过时：专项要两个以上话题，这条不再提。", "lesson": False}
            proposals.update_proposal(state, record["id"], lambda data: data.update(state=proposals.EXPIRED,
                                                                                      decided_at=time.time(), view=view))
            self.proposal_cards.patch_own(ctx, str((record.get("card") or {}).get("message_id") or ""), cards.proposal_card(view))
            gone += 1
        return gone

    def send_tidy(self, ctx, chat, run, view) -> str:
        sent = []
        self.messenger.send_message(ctx.profile, chat, "整理专项：正在整理", self.voice.safe_key(self.voice.key("tidy-run", run)),
                                    voice=self.voice, card=cards.tidy_card(view), on_card=sent.append)
        return sent[0] if sent else ""

    def save_run(self, state, record) -> None:
        path = facts.run_path(state, record["run"])
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(path, record, ensure_ascii=False, indent=1)

    def redraw(self, ctx, record) -> None:
        if record.get("message_id"):
            self.messenger.patch_card(ctx.profile, record["message_id"], cards.tidy_card(record["view"]))
