"""A whole 整理 (scheduled or asked for) and the 整理专项 card it draws: the run's record, its progress, the judge's suggestions as
rows (docs/projects.md 整理专项). Joins and folds through app/tidy_apply.py, proposals through app/tidy_proposals.py, the rows' words
from app/tidy_words.py; the clicks on its card are app/tidy_clicks.py's, above it."""
from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

from covey.app import assist
from covey.app import project_sorter as sorter
from covey.app import tidy_facts as facts
from covey.cards import project_proposal as cards
from covey.core import assist as reasons
from covey.core import project_proposals as proposals
from covey.core import projects
from covey.core.state import write_json
from covey.core.topics import label_title

PROGRESS_GAP = 3.0  # Seconds between two redraws of the card while it reads.
BUSY_WORDS = proposals.BUSY_PREFIX + "（{done}/{total}）"
FEW_WORDS = "话题还不多（不到 {} 个），先不整理专项。"
OFF_WORDS = "整理专项没有开（COVEY_PROJECT_TIDY）。"
UNREADABLE_WORDS = {"model_error": "模型返回了错误", "refused": "官方内容安全拦了", "policy_refusal": "官方内容安全拦了",
                    "too_long": "内容太长，一次读不完", "root_failed": "模型返回了错误",
                    "exit_nonzero": "读它的进程出错退出了", "timeout": "读超时了",
                    "bad_output": "模型的回答看不懂", "empty": "里面还没有你说的话", "redacted": "开了脱敏，内容不外发",
                    "no_backend": "没有能读它的模型（Claude Code、Codex、Antigravity 都没启用，也没设模型网关）",
                    "not_signed_in": "Antigravity 没登录", "error": "读的时候出错"}
FAILED_WORDS = {"timeout": "判断超时了",
                "no_backend": "没有能判断的模型（Claude Code、Codex、Antigravity 都没启用）",
                "bad_output": "判断的回答看不懂", "model_error": "模型返回了错误", "exit_nonzero": "判断的进程出错退出了",
                "spawn_failed": "判断的进程没起来", "not_signed_in": "判断用的 Antigravity 没登录",
                "policy_refusal": "被官方内容安全拦了", "refused": "被官方内容安全拦了", "too_long": "要读的内容太长",
                "none": "辅助模型选了不用模型", "not_allowed": "设置里选的辅助模型用不了"}
MANUAL = ("asked", "settings", "web", "butler")  # Origins the owner asked for: a 15-minute break does not stop them.


def failed_words(code, harness="", detail="") -> str:
    """The 整理专项 card's words for a run that failed on ``code`` through ``harness`` (core/assist.py words else);
    ``detail``: the reason under a Harness that is unusable or broken now (a login, a spent quota)."""
    from covey.app.assist import name_of
    said = FAILED_WORDS.get(code) or reasons.words(code)
    if code in ("unusable", "broken") and detail in reasons.WORDS:
        said = f"现在用不了（{reasons.words(detail)}）"
    return f"{name_of(harness)} {said}" if harness and code not in ("none", "no_backend") else said


def last_assist(state) -> dict:
    """The latest 补话题要点 call's {harness, model} (core/assist.py recent: kept under 整理专项's row, app/assist.py BACKFILL),
    for a run that stopped reading."""
    row = (reasons.read(state).get("recent") or {}).get("tidy") or {}
    return {"harness": str(row.get("harness") or ""), "model": str(row.get("model") or "")}


class Runs:
    """The whole 整理. ``runtime``: the log and the owner's chat. ``messenger``, ``voice``: the 整理专项 card. ``env``: the judge's
    tools, the state directory. ``shared``: app/tidy_state.py TidyState (``busy``, ``progress``, ``topic_count``). ``words``:
    app/tidy_words.py TopicWords. ``applier``: app/tidy_apply.py Applier. ``proposal_cards``: app/tidy_proposals.py ProposalCards."""

    def __init__(self, runtime, messenger, voice, env, shared, words, applier, proposal_cards) -> None:
        self.runtime, self.messenger, self.voice, self.env = runtime, messenger, voice, env
        self.shared, self.words, self.applier, self.proposal_cards = shared, words, applier, proposal_cards

    def scheduled(self, ctx, chat, now) -> None:
        """整理 at the 早晚汇总's times (COVEY_DIGEST_TIMES), each time once (its own ``last_slot``). Not while the 辅助模型 is
        不用模型: a 整理 is the judge's call, there is no rules-only one, so the card would only say it failed (one asked for
        still says why)."""
        from covey.app import assist
        from covey.app.digest import configured, due

        times = configured()
        if not times:
            return
        slot = due(times, now, proposals.load(ctx.state_dir).get("last_slot"))
        if slot is None or self.shared.busy.locked():
            return
        proposals.change(ctx.state_dir, lambda data: data.update(last_slot=slot))
        if assist.settings_of(ctx.state_dir).assist.off:
            self.runtime.log(f"project tidy scheduled skipped slot={slot} reason=none")
            return
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
        return proposals.WEB_STARTED_WORDS if origin == "web" else proposals.STARTED_WORDS

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
        where = sorter.Where(getattr(self.env, "topics", None), assist.TIDY, manual=record.get("origin") in MANUAL)
        missing = [topic for topic in topics if sorter.needs_gist(state, topic, sorter.material(state, chat, topic, now).last_job)]
        self.shared.progress = {"done": 0, "total": len(missing)}
        drawn, unreadable = 0.0, []
        for number, topic in enumerate(missing, 1):
            label = label_title(state, topic)[0] or "话题"
            self.shared.progress = {"done": number, "total": len(missing)}
            view["progress"] = {"done": number, "total": len(missing), "current": label}
            if time.monotonic() - drawn >= PROGRESS_GAP:
                self.redraw(ctx, record)
                self.save_run(state, record)  # The web page follows the reading (app/tidy_web.py stamps).
                drawn = time.monotonic()
            # Each read routed anew (app/assist.py): never a backend kept from before the loop.
            reason = sorter.backfill(state, chat, topic, where=where, log=self.runtime.log, now=now)
            if sorter.stops(reason):  # No model can read any of them now: the 整理 stops here and says why.
                self.failed(view, reason, last_assist(state))
                return
            if reason in sorter.TOPIC_PROBLEMS:
                unreadable.append((topic, reason))
        topics = [topic for topic in topics if topic not in {topic for topic, _ in unreadable}]
        # Which ones and why.
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
        verdict = sorter.judge(state, records, items, where=where, scope=record.get("scope") or "",
                               lessons=proposals.lessons(state), run=record["run"], log=self.runtime.log)
        if verdict.why not in ("ok", "no_topics"):
            self.failed(view, verdict.why, {"harness": verdict.via, "model": verdict.model, "detail": verdict.detail})
            return
        self.settle(ctx, chat, record, verdict.suggestions)
        self.expire_alone(ctx)
        view["state"] = "done"

    def failed(self, view, code, used) -> None:
        """The run's view failed on ``code`` (core/assist.py's reason), ``used``: {harness, model, detail} of the call."""
        harness, detail = str((used or {}).get("harness") or ""), str((used or {}).get("detail") or "")
        view.update(state="failed", why=failed_words(code, harness, detail), why_code=code, via=harness,
                    model=str((used or {}).get("model") or ""), detail=detail,
                    retry=reasons.kind_of(code) == "passing")

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
                # Each new project its own proposal card (each topic
                # linked with its gist, topics added or removed and the name changed before it is confirmed): the 整理专项 card only names it.
                if self.proposal_cards.propose(ctx, chat, {**made, "run": record["run"]}) == "proposed":
                    view.setdefault("proposed", []).append(str(made.get("name") or ""))
                else:
                    # The same proposal already waits for him (not dropped as
                    # nothing new): named as still waiting, to decide on the 看板.
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
