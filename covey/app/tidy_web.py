"""整理专项 on the web page: when its state last changed, the panel's view, and a 好 or 不用 from the page, which goes through the
click of the card (docs/butler.md 网页工作台)."""
from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

from covey.cards import project_proposal as cards
from covey.core import project_proposals as proposals
from covey.core.state import read_object

WEB_RUNS, WEB_OPEN_DAYS, WEB_DECIDED_HOURS = 30, 7, 24  # The web page's 整理专项: the runs looked at, how long open and decided ones stay.
WEB_STATES = {proposals.ACCEPTED: "done"}  # Being carried out: no buttons; the next look says done or failed.
WEB_DOING = "正在照办……"
WEB_REFUSED = {"tidy_stale": "这条建议的卡已经过时了", "tidy_running": "正在整理，等这次整理完再定", "tidy_decided": "这条已经定过了",
               "tidy_bad_value": "这条建议找不到了", "tidy_no_topics": "没有话题，专项没建"}


class WebSide:
    """``ProjectTidy``'s web part. Uses ``busy``, ``progress``, ``env``, ``chat``, ``decide``, ``row_click`` of app/project_tidy.py ProjectTidy."""

    # The web page -------------------------------------------------------------------------------------------------------

    def web_stamps(self) -> tuple:
        """When the runs and the proposals last changed (their folders' mtimes: every write is a rename into them), so the web
        page's 整理专项 is built again only then (app/web_sync.py push_tidy)."""
        found = []
        for folder in (proposals.folder(Path(self.env.state_dir)) / "runs", proposals.proposals_dir(Path(self.env.state_dir))):
            try:
                found.append(folder.stat().st_mtime_ns)
            except OSError:
                found.append(0)
        return (*found, self.busy.locked())

    def web_view(self, now=None) -> dict | None:
        """The 整理专项 panel of the web page's 看板 (data contract v16, ``settings`` item ``tidy``): the suggestions as the 整理专项 card
        draws them (cards/project_proposal.py suggestion_words) and the 新建 proposals waiting on a card of their own; the ones still
        open from the runs of the last WEB_OPEN_DAYS, the decided ones for WEB_DECIDED_HOURS. None with 整理专项 off."""
        if not proposals.enabled() or not self.env.state_dir:
            return None
        state, now = Path(self.env.state_dir), time.time() if now is None else now
        ctx = SimpleNamespace(state_dir=state, profile=getattr(self.env, "profile", None))
        shown, seen = [], set()

        def put(record, found, run, note=""):
            status = record.get("state") or proposals.OPEN
            if status != proposals.OPEN and now - float(record.get("decided_at") or 0) > WEB_DECIDED_HOURS * 3600:
                return
            seen.add(record["id"])
            words = cards.suggestion_words(found)
            shown.append({"id": record["id"], "run": run, "kind": found["kind"], **words,
                          "why": found.get("why") or "", "goal": found.get("goal") or "",
                          "topics": [{"key": each["key"], "label": each["label"], "title": each["title"]} for each in found["topics"]],
                          "state": WEB_STATES.get(status, status), "note": "" if status == proposals.OPEN else
                          (record.get("note") or note or (record.get("view") or {}).get("note") or cards.OUTCOME.get(status, "")
                           or (WEB_DOING if status == proposals.ACCEPTED else "")),
                          "at": float(record.get("created_at") or 0)})

        try:
            paths = sorted((proposals.folder(state) / "runs").glob("*.json"), reverse=True)[:WEB_RUNS]
        except OSError:
            paths = []
        for path in paths:
            run = read_object(path)
            if now - float(run.get("started_at") or 0) > WEB_OPEN_DAYS * 86400:
                break
            for row in (run.get("view") or {}).get("rows") or ():
                pid = row.get("proposal")
                if not row.get("item") or pid in seen:
                    continue  # A row kept before ``item`` (its words are the card's line alone) stays on its card.
                record = proposals.read_proposal(state, pid)
                if record.get("id") == pid:
                    put(record, row["item"], run.get("run") or path.stem, row.get("note") or "")
        decided = [record for record in proposals.proposals(state, (proposals.DONE, proposals.FAILED, proposals.DECLINED,
                                                                     proposals.NEVER))
                   if record.get("kind") == proposals.CREATE and (record.get("card") or {}).get("message_id")]
        for record in [*proposals.waiting(state), *decided]:
            if record["id"] not in seen and (record.get("state") == proposals.OPEN
                                             or now - float(record.get("decided_at") or 0) <= WEB_DECIDED_HOURS * 3600):
                put(record, self.row_item(ctx, record), "")
        shown.sort(key=lambda each: (each["state"] != "open", -each["at"]))
        running = dict(self.progress) if self.busy.locked() else None
        return {"proposals": shown, "running": running, "updated_at": now}

    def web_decide(self, pid, choice) -> tuple:
        """好 (``choice`` "ok") or 不用 ("decline") on one suggestion, from the web page (app/web_ops.py ``tidy_decide``): through the
        click of its card (a 整理专项 card's row, or a 新建 proposal's own card), which is redrawn the same. (whether it was done, the
        words: what came of it, or why nothing was done)."""
        state, pid = Path(self.env.state_dir), str(pid or "")
        record = proposals.read_proposal(state, pid) if pid.isalnum() else {}
        if record.get("id") != pid:
            return False, "这条建议找不到了"
        if record.get("state") != proposals.OPEN:
            return False, "这条已经定过了：" + (record.get("note") or cards.OUTCOME.get(record.get("state"), "") or "已处理")
        chat = self.chat()
        if not chat:
            return False, "还不知道你的私聊，先在飞书里和机器人说句话"
        ctx, ok = SimpleNamespace(state_dir=state, profile=getattr(self.env, "profile", None)), choice == "ok"
        own = str((record.get("card") or {}).get("message_id") or "")
        if record.get("kind") == proposals.CREATE and own:
            outcome = self.decide(ctx, chat, {}, {"kind": cards.CREATE_KIND if ok else cards.DECLINE_KIND, "proposal": pid}, own)
        else:
            run = str(record.get("run") or "")
            saved = read_object(self.run_path(state, run)) if run.replace("-", "").isalnum() else {}
            if not saved:
                return False, "这条建议的整理卡找不到了"
            outcome = self.row_click(ctx, chat, {}, {"kind": cards.OK_KIND if ok else cards.DECLINE_KIND, "run": run, "proposal": pid},
                                     str(saved.get("message_id") or ""))
        if outcome in WEB_REFUSED:
            return False, WEB_REFUSED[outcome]
        after = proposals.read_proposal(state, pid)
        status = after.get("state")
        note = after.get("note") or (after.get("view") or {}).get("note") or ""
        if status == proposals.DONE:
            return True, note or "已照办"
        if status in (proposals.DECLINED, proposals.NEVER):
            return True, "不用：这条不会再这样提"
        if status == proposals.FAILED:
            return False, "没做成：" + (note or "出错了")
        return False, "这条已经定过了"
