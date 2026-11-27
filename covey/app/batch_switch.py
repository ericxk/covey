"""批量换配置 (T786): the butler or a project's lead proposes switching several topics to another Harness, model or budget at once,
and the owner confirms it on one card (cards/batch_switch.py) in the private chat. Before it, each topic had to be switched by hand
on its own 🔁 换配置 card.

- ``topics_switch`` (the butler's tool, and a lead's: core/butler_tool_specs.py): the topics (T numbers, up to ``MOST``), what to
  switch to (``harness``, ``model``, ``effort``: a field left out keeps the topic's own, or the settings' default on another
  Harness, as app/butler_model_choice.py ``resolve`` finds it with the topic's binding as its defaults) and ``basis``, the
  owner's words it is on (app/config_basis.py ``check``: a call on no words of the owner's is refused). Each topic is planned
  (``plan``): one it cannot switch (not found, the butler's or a lead's own, a topic of another project for a lead, the same
  configuration already, a Harness that cannot redact for a topic that does, one its device does not declare) is a row that
  says why and is never switched. The record is written first (<state>/batch-switch/<token>.json, the request's token: the
  same request again sends nothing), then the card goes out; the tool answers at once and nothing is switched yet.
- ``claim`` (app/unified.py PANELS): 确认更换, 取消 and 再试没换的, the owner's only, once per event; the switching runs after the
  click is acknowledged (app/card_lanes.py ``after_ack``), one topic after another through app/binding.py ``switch_topic``
  (the same rebind and checks as 🔁 换配置: a topic with a task queued or running is refused, its questions of the old binding
  are void, its next turn gets the handoff). A row is marked ``switching`` before its switch and ``done`` or ``failed`` after;
  a topic whose binding changed since the card was drawn is ``stale`` and left alone. Each switched topic hears it in its thread.
  No record lock is held across a switch or a card write (AGENTS.md rules 1 and 2).
- What is left mid-way (WAVE12 C10, T904; tests/golden/midstates.json): the card's send is ``sending`` before it goes, then
  ``pending`` with its message, ``failed`` when it surely did not go (the same request sends it again) or ``uncertain`` when it
  may have gone (never sent again by itself). A click on a card whose record has no message yet (``sending``, ``uncertain``: the
  Bridge stopped before it wrote it, or Feishu never confirmed it) is the proof it went: the record takes its message and is
  ``pending``. ``running`` names its process (``owner``, core/process.py process_identity); one whose process is gone (a restart)
  is settled (``settle_left``) at the Bridge's start and at the next click: rows it left ``switching`` or never reached are
  ``failed`` (再试没换的 tries them), the record ``done``, its card redrawn. ``switch_all`` leaves ``running`` whatever happens.
"""
from __future__ import annotations

import functools
import os
import threading
import time
from pathlib import Path

from covey.app import config_basis
from covey.app.butler_env import arg_text, reply
from covey.app.card_lanes import after_ack
from covey.cards import batch_switch as cards
from covey.core import butler, butler_settings, devices, projects
from covey.core.butler_opened import request_token
from covey.core.harness import backend_capabilities
from covey.core.locks import resource_lock
from covey.core.process import is_owned, process_identity
from covey.core.redaction import TOPIC_KEY as REDACTION_KEY
from covey.core.state import claim_once, keyed_json_path, read_object, write_json
from covey.core.topics import topic_label, topic_title
from covey.feishu.messenger import unsent_outcome
from covey.feishu.transport import NOT_SENT

FOLDER = "batch-switch"
INBOUND = "inbound-messages"
MOST = 20  # Topics one call may switch.
NO_CHAT = "还不知道主人和机器人的私聊（主人还没在私聊里发过消息），没有执行。"
SENT = ("已在私聊发出批量换配置卡：{count} 个话题，主人点「确认更换」后才换，结果写在那张卡上。{skipped}"
        "告诉主人确认卡在私聊里，别说已经换了。")
UNCERTAIN_SENT = ("批量换配置卡可能已经发到私聊（飞书没有确认），什么都还没换。告诉主人去私聊找这张卡，找到就点「确认更换」；"
                  "找不到就请他再说一次，别说已经换了。")
# What a row says when a switching stopped part way (``settled``): at a restart, and at an error that stopped ``switch_all``.
LEFT_WHY = ("换的时候 Bridge 重启了，可能没换成：先在话题里看一眼，再点「再试没换的」",
            "Bridge 重启前还没轮到它，没有换：点「再试没换的」接着换")
BROKEN_WHY = ("换的时候出错，可能没换成：先在话题里看一眼，再点「再试没换的」",
              "出错停下时还没轮到它，没有换：点「再试没换的」接着换")


def path(state_dir, token) -> Path:
    return keyed_json_path(Path(state_dir), FOLDER, token)


def save(target, record) -> None:
    write_json(target, record, ensure_ascii=False, indent=2)


def label(app, model, effort, work_mode="") -> str:
    """A binding as the cards name it: Harness · provider · model · 思考预算 X."""
    named = app.cards.model_label(app.harness.context, model) if model else ""
    named = named or f"{app.spec.title} · {model or '默认模型'}"
    return f"{named} · 思考预算 {effort or '模型默认'}" + (f" · {work_mode}" if work_mode else "")


def current(app, topic) -> dict:
    """The topic's binding as a row's ``from``."""
    return {"harness_id": str(topic.get("harness_id") or ""), "revision": int(topic.get("binding_revision", 1) or 1),
            "model": str(topic.get("model") or ""), "effort": str(topic.get("effort") or ""),
            "work_mode": str(topic.get("work_mode") or ""),
            "label": label(app, str(topic.get("model") or ""), str(topic.get("effort") or ""))}


def refusal(topic, name, app) -> str:
    """Why ``topic`` cannot run on Harness ``name`` (``app``) at all, '' when it can: 敏感词脱敏 kept, its device's Harnesses."""
    if topic.get(REDACTION_KEY) is True and not backend_capabilities(app.backend, app.harness.context).redaction:
        return f"这个话题开着敏感词脱敏，{app.spec.title} 不能脱敏"
    where = str(topic.get("workdir") or "")
    if devices.is_device_value(where):
        refused = devices.undeclared_refusal(where.split(":", 1)[0], name, app.spec.title)
        if refused:
            return refused.replace("任务尚未启动", "没有换")
    return ""


def me() -> dict | None:
    """This process, as a ``running`` record names its ``owner`` (core/process.py process_identity)."""
    return process_identity(os.getpid())


def left(record) -> bool:
    """Whether the ``running`` record's switching is no process's now: its ``owner`` is not alive as another process (it is
    gone, a restart; or it is this one, which runs one token's switching at a time and is past it; or an older release's
    record names none)."""
    owner = record.get("owner")
    return not (isinstance(owner, dict) and owner != me() and is_owned(owner))


def settled(record, why=LEFT_WHY) -> dict:
    """``record`` once its switching is over: ``done``, and when it stopped part way (``why``: what a row it left
    ``switching`` says, then one it never reached) those rows failed, so 再试没换的 tries them, and ``settled_at``."""
    rows, changed = [], False
    for row in record.get("rows") or ():
        if row.get("state") in ("switching", "ready"):
            row, changed = {**row, "state": "failed", "why": why[row["state"] == "ready"]}, True
        rows.append(row)
    return {**record, "rows": rows, "status": "done", **({"settled_at": time.time()} if changed else {})}


def sent_answer(record) -> dict:
    """The tool's answer once the card of ``record`` is out (its rows' count and what is not switched)."""
    ready = [row for row in record["rows"] if row["state"] == "ready"]
    skipped = [row for row in record["rows"] if row["state"] == "skip"]
    said = "；".join(f"{row.get('label') or row['name']}：{row['why']}" for row in skipped)
    return reply(True, SENT.format(count=len(ready), skipped=f"这几个不换：{said}。" if said else ""),
                 count=len(ready), skipped=len(skipped))


def plan(env, chat, name, args, lead=None, choices=None) -> dict:
    """The row of the topic ``name`` (a T number): ``from`` and ``to`` (Harness, model, budget, work mode and their label) and
    ``state`` ready, or skip with ``why``."""
    from covey.app.butler_model_choice import harness_id, resolve
    from covey.app.butler_topics import topic_ref

    row = {"name": arg_text(name, 40), "state": "skip", "why": ""}
    found = topic_ref(env, chat, name)
    if isinstance(found, str):
        return {**row, "label": row["name"], "from": {"label": "—"}, "why": found.rstrip("。")}
    topic_id, (app, _ctx, topic) = found
    state = Path(env.state_dir)
    row.update(topic_id=topic_id, label=topic_label(state, topic_id) or row["name"], title=topic_title(state, topic_id)[:40],
               **{"from": current(app, topic)})
    if butler.is_lead(topic_id, chat):
        return {**row, "why": "这是专项的负责人：它的配置在专项卡的「换配置」上换"}
    if lead is not None:
        member = projects.topic_project(Path(env.state_dir), topic_id)
        if member is None or member.id != lead[0]:
            return {**row, "why": "不是这个专项的话题"}
    harness = harness_id(env.topics, arg_text(args.get("harness"))) or row["from"]["harness_id"]
    if harness == row["from"]["harness_id"] and not arg_text(args.get("model")):
        # The same Harness and model: the topic's own, kept as it is (not looked up on today's menu); a budget it names is checked.
        app = env.topics.apps.get(harness)
        model, mode = row["from"]["model"], row["from"]["work_mode"]
        effort = arg_text(args.get("effort")) or row["from"]["effort"]
        entry = (choices or {}).get(f"{harness}:{model}") or {}
        if arg_text(args.get("effort")) and entry.get("levels") and effort not in entry["levels"]:
            return {**row, "why": f"{model} 没有思考预算「{effort}」，可选：{'、'.join(entry['levels'])}"}
        model_key = f"{harness}:{model}" if entry else ""
    else:
        # Another Harness or model: found as topic_start finds one (the call's, else the 设置 card's 新话题 defaults, else the
        # chat's last 开始任务, else the Bridge's own), so its cards name a model and a budget.
        asked = {field: args.get(field) for field in config_basis.FIELDS if arg_text(args.get(field))}
        resolved = resolve(env.topics, env.state_dir, chat, butler_settings.load(env.state_dir).topic_defaults,
                           {**asked, "harness": harness}, choices)
        if isinstance(resolved, str):
            return {**row, "why": resolved.rstrip("。")}
        harness, model, effort, model_key = (resolved["harness"], resolved["model"], resolved["effort"],
                                             resolved["model_key"])
        app = resolved["app"]
        mode = row["from"]["work_mode"] if harness == row["from"]["harness_id"] else str(
            getattr(resolved["base"], "work_mode", "") or "")
    if app is None:
        return {**row, "why": f"本机器人没有启用 {harness}"}
    new = app
    row["to"] = {"harness": harness, "model": model, "effort": effort, "work_mode": mode, "model_key": model_key,
                 "label": label(new, model, effort)}
    if (harness, model, effort, mode) == tuple(row["from"][key] for key in ("harness_id", "model", "effort", "work_mode")):
        return {**row, "why": "已经是这个配置"}
    refused = refusal(topic, harness, new)
    if refused:
        return {**row, "why": refused}
    return {**row, "state": "ready"}


def topics_switch(env, request):
    """The tool: plan each topic, keep the record, send the card; nothing is switched (module docstring)."""
    from covey.app.butler_topics import lead_of
    from covey.app.launch_models import available_models

    chat = env.chat()
    if not chat:
        return reply(False, NO_CHAT)
    args = request.args
    names = args.get("topics") if isinstance(args.get("topics"), list) else []
    names = [arg_text(name, 40) for name in names if arg_text(name)]
    if not names:
        return reply(False, "topics 要列出至少一个话题的 T 号。")
    if len(names) > MOST:
        return reply(False, f"一次最多换 {MOST} 个话题，分几次发。")
    if not config_basis.named(args):
        return reply(False, "要写换成什么：harness、model、effort 至少填一个。")
    lead = lead_of(env, request)
    shown, refused = config_basis.check(env.state_dir, request.job_key, args.get("basis"), project_id=lead[0] if lead else "")
    if refused:
        return reply(False, refused)
    token = request_token(request)
    target = path(env.state_dir, token)
    record = read_object(target)
    if record.get("answer"):
        return record["answer"]  # The same request: the card is out already.
    if not record.get("rows"):
        default = env.topics.apps.get(env.topics.default) or next(iter(env.topics.apps.values()))
        choices = available_models(env.topics, default.models.configured_context(default.harness.context))
        rows, seen, ids = [], set(), numbered(env.state_dir, chat, names)
        for name in names:
            row = plan(env, chat, ids.get(name, name), args, lead, choices)
            row["name"] = name
            if row.get("topic_id") and row["topic_id"] in seen:
                continue  # The same topic twice.
            seen.add(row.get("topic_id") or row["name"])
            rows.append(row)
        record = {"token": token, "request": request.id, "job_key": request.job_key, "chat_id": chat, "status": "sending",
                  "basis": shown, "rows": rows, "at": time.time(), "message_id": "",
                  **({"lead": {"project_id": lead[0], "name": lead[1]}} if lead else {})}
        save(target, record)
    ready = [row for row in record["rows"] if row["state"] == "ready"]
    skipped = [row for row in record["rows"] if row["state"] == "skip"]
    said = "；".join(f"{row.get('label') or row['name']}：{row['why']}" for row in skipped)
    if not ready:
        answer = reply(False, "没有能换的话题，没发确认卡：" + said + "。")
        save(target, {**record, "status": "nothing", "answer": answer})
        return answer
    sent = []
    key = env.voice.safe_key(f"batch-switch-{token}")
    if record.get("status") != "sending":  # A request whose send failed before (``failed``): tried again under its key.
        record = {**record, "status": "sending"}
        save(target, record)
    ok = env.messenger.send_message(env.profile, chat, cards.text(record), key, voice=env.voice,
                                    card=cards.card({**record, "status": "pending"}), on_card=sent.append)
    if not ok and unsent_outcome(env.messenger) != NOT_SENT:
        # It may have gone (rule 1): never sent again by itself; a click on it is the proof it went (``claim``).
        answer = reply(False, UNCERTAIN_SENT)
        if not finish(target, status="uncertain", answer=answer):
            return read_object(target).get("answer") or sent_answer(read_object(target))
        env.runtime.log(f"batch switch card uncertain token={token[:12]}")
        return answer
    if not ok or not sent:
        # Surely not sent, or gone as Markdown (no card to click): the same request sends it again.
        if not finish(target, status="failed"):
            return read_object(target).get("answer") or sent_answer(read_object(target))
        return reply(False, "批量换配置卡没有发出去（飞书拒绝了卡片），什么都没换；可以稍后再试。")
    answer = sent_answer(record)
    if not finish(target, status="pending", message_id=str(sent[0]), answer=answer):
        return read_object(target).get("answer") or answer  # A click took the card first (``claim``).
    env.runtime.log(f"batch switch card sent token={token[:12]} topics={len(ready)} skipped={len(skipped)}")
    return answer


def finish(target, **fields) -> bool:
    """The record at ``target`` given ``fields`` once its card's send is over, under its lock, while it is still ``sending``
    (a click on the card may have taken it already, ``claim``): whether it was."""
    with resource_lock("batch-switch", target):
        record = read_object(target)
        if record.get("status") != "sending":
            return False
        save(target, {**record, **fields})
    return True


def numbered(state_dir, chat, names) -> dict:
    """{name: topic id} of the names that are T numbers of ``chat``'s topics, in one pass over the topics (app/butler_topics.py
    ``topic_ref`` reads them all for each number; a batch names up to MOST)."""
    import re

    wanted = {}
    for name in names:
        found = re.fullmatch(r"[Tt]?(\d+)", name)
        if found:
            wanted.setdefault(int(found.group(1)), []).append(name)
    ids = {}
    if not wanted:
        return ids
    for topic_json in (Path(state_dir) / "topics").glob("*/topic.json"):
        data = read_object(topic_json)
        number = data.get("topic_number")
        if type(number) is int and number in wanted and data.get("chat_id") == chat and data.get("topic_id"):
            for name in wanted.pop(number):
                ids[name] = str(data["topic_id"])
            if not wanted:
                break
    return ids


TOOLS = {"topics_switch": topics_switch}


class BatchSwitch:
    """The card's clicks and the switching (module docstring). ``topics``: app/topics.py Topics."""

    def __init__(self, runtime, topics, messenger) -> None:
        self.runtime, self.topics, self.messenger = runtime, topics, messenger
        self.lock = threading.Lock()
        self.busy = set()  # Tokens whose switching runs now.

    @staticmethod
    def matches(raw) -> bool:
        return isinstance(raw, dict) and raw.get("kind") in cards.KINDS

    def claim(self, ctx, event, raw, owner):
        """A 批量换配置 card's click (app/unified.py PANELS), else None."""
        if not self.matches(raw):
            return None
        if not owner:
            return "owner_only"
        event_id = str(event.get("event_id") or "")
        if not event_id or not claim_once(Path(ctx.state_dir) / INBOUND, "batch-switch-" + event_id):
            return "duplicate"
        token, message_id = str(raw.get("batch") or ""), str(event.get("message_id") or "")
        target = path(ctx.state_dir, token) if token else None
        record = read_object(target) if target is not None else {}
        if record and not record.get("message_id") and record.get("chat_id") == str(event.get("chat_id") or ""):
            record = self.adopt(target, message_id) or record
        if (not record or record.get("message_id") != message_id
                or record.get("chat_id") != str(event.get("chat_id") or "")):
            return "batch_switch_stale"
        kind = raw["kind"]
        with self.lock:
            if token in self.busy:
                return "batch_switch_busy"
            self.busy.add(token)
        after_ack(functools.partial(self.decide, ctx, target, kind, token))
        return {cards.CONFIRM: "batch_switch_confirm", cards.CANCEL: "batch_switch_cancel"}.get(kind, "batch_switch_retry")

    @staticmethod
    def adopt(target, message_id) -> dict | None:
        """The record at ``target`` with ``message_id``, the card a click came from, when it has no message yet and its send
        is ``sending`` or ``uncertain`` (the click is the proof it went): ``pending``. None when it is not such a record."""
        if not message_id:
            return None
        with resource_lock("batch-switch", target):
            record = read_object(target)
            if record.get("message_id") or record.get("status") not in ("sending", "uncertain") or not record.get("rows"):
                return None
            record = {**record, "status": "pending", "message_id": message_id,
                      "answer": record.get("answer") or sent_answer(record)}
            save(target, record)
        return record

    def settle_left(self, ctx, target) -> bool:
        """The record at ``target`` settled when it is ``running`` and its process is gone (``left``, ``settled``), its card
        redrawn: whether it was."""
        with resource_lock("batch-switch", target):
            record = read_object(target)
            if record.get("status") != "running" or not left(record):
                return False
            record = settled(record)
            save(target, record)
        self.runtime.log(f"batch switch settled after a restart token={str(record.get('token'))[:12]}")
        self.paint(ctx, record)
        return True

    def settle_all(self, ctx) -> int:
        """At the Bridge's start: every ``running`` record whose process is gone settled (``settle_left``); how many."""
        folder = Path(ctx.state_dir) / FOLDER
        count = 0
        for target in sorted(folder.glob("*.json")) if folder.is_dir() else ():
            if read_object(target).get("status") == "running":
                try:
                    count += self.settle_left(ctx, target)
                except Exception as exc:  # noqa: BLE001 - one record's trouble; the next click settles it
                    self.runtime.log(f"batch switch not settled at start: {type(exc).__name__}")
        return count

    def decide(self, ctx, target, kind, token) -> str:
        """What the click does, run after its acknowledgement: the outcome ('cancelled', 'switched', 'ignored')."""
        try:
            with resource_lock("batch-switch", target):
                record = read_object(target)
                if record.get("status") == "running" and left(record):
                    # Its switching stopped with its process (``busy`` keeps this one's own click off): settled first,
                    # so 再试没换的 goes on with it.
                    record = settled(record)
                    save(target, record)
                    self.runtime.log(f"batch switch settled after a restart token={str(record.get('token'))[:12]}")
                status = record.get("status")
                if kind == cards.CANCEL and status == "pending":
                    record = {**record, "status": "cancelled"}
                    save(target, record)
                elif (kind == cards.CONFIRM and status == "pending") or (kind == cards.RETRY and status == "done"):
                    record = {**record, "status": "running", "owner": me()}
                    save(target, record)
                else:
                    record = None  # A second click, or one on a card already decided (or another process's): redrawn.
            if record is None:
                self.paint(ctx, read_object(target))
                return "ignored"
            self.paint(ctx, record)
            if record["status"] == "cancelled":
                self.runtime.log(f"batch switch cancelled token={record['token'][:12]}")
                return "cancelled"
            self.switch_all(ctx, target, record)
            return "switched"
        finally:
            with self.lock:
                self.busy.discard(token)

    def paint(self, ctx, record) -> None:
        if record.get("message_id"):
            self.messenger.patch_card(ctx.profile, record["message_id"], cards.card(record))

    def put_row(self, target, index, **fields) -> dict:
        with resource_lock("batch-switch", target):
            record = read_object(target)
            record["rows"][index] = {**record["rows"][index], **fields}
            save(target, record)
        return record

    def switch_all(self, ctx, target, record) -> None:
        chat = record["chat_id"]
        try:
            for index, row in enumerate(record["rows"]):
                if row.get("state") not in ("ready", "failed", "switching"):
                    continue
                self.put_row(target, index, state="switching", why="")  # Before the switch: a restart settles it.
                try:
                    state, why = self.switch_one(ctx, chat, row)
                except Exception as exc:  # noqa: BLE001 - one topic's trouble: said on its row, the others go on
                    self.runtime.log(f"batch switch failed topic={str(row.get('topic_id'))[:12]}: {type(exc).__name__}")
                    state, why = "failed", f"换的时候出错（{type(exc).__name__}），可能没换成：先在话题里看一眼再试"
                self.put_row(target, index, state=state, why=why)
        finally:
            # Never left ``running``: what an error stopped part way is settled as a restart is.
            with resource_lock("batch-switch", target):
                record = read_object(target)
                if record.get("status") == "running":
                    record = settled(record, BROKEN_WHY)
                    save(target, record)
            try:  # Its card says what came of it, an error that stopped it included.
                self.paint(ctx, record)
            except Exception as exc:  # noqa: BLE001 - the record says it; the next click redraws it
                self.runtime.log(f"batch switch card not redrawn: {type(exc).__name__}")
        numbers = cards.counted(record)
        self.runtime.log(f"batch switch done token={record['token'][:12]} done={numbers.get('done', 0)} "
                         f"failed={numbers.get('failed', 0)} stale={numbers.get('stale', 0)}")

    def switch_one(self, ctx, chat, row) -> tuple[str, str]:
        """(state, why) of one row's switch."""
        from covey.app.binding import UNCHANGED, switch_topic

        found = self.topics.target(Path(ctx.state_dir), row["topic_id"], chat)
        if found is None:
            return "stale", "话题已不在主人私聊里，或它的 Harness 没有启用"
        app, topic_ctx, topic = found
        was, to = row["from"], row["to"]
        now = (str(topic.get("harness_id") or ""), str(topic.get("model") or ""), str(topic.get("effort") or ""),
               str(topic.get("work_mode") or ""))
        if now == (to["harness"], to["model"], to["effort"], to["work_mode"]):
            return "done", ""  # Switched already (a click before a restart got this far).
        if (topic.get("harness_id"), int(topic.get("binding_revision", 1) or 1)) != (was["harness_id"], was["revision"]):
            return "stale", "卡发出后话题的配置已经变了，这张卡不再动它"
        new = self.topics.apps.get(to["harness"])
        if new is None:
            return "failed", f"本机器人没有启用 {to['harness']}"
        refused = refusal(topic, to["harness"], new)
        if refused:
            return "failed", refused
        form = {"harness_id": was["harness_id"], "binding_revision": was["revision"], "job_id": None}
        try:
            switched = switch_topic(self.topics, topic_ctx, chat, form, to["harness"], to["model"], to["effort"],
                                    to["work_mode"])
        except ValueError as exc:
            reason = str(exc)
            if reason == UNCHANGED:
                return "done", ""
            # 换配置's words for its own card (app/binding.py busy_reason) said as this card's next step.
            for words in ("请先在任务卡上停止，停止完成后再确认更换", "等它做完再确认更换", "完成后再确认更换",
                          "请在最新的任务卡上重新打开换配置"):
                reason = reason.replace(words, "等它停了再点「再试没换的」")
            return "failed", reason
        if switched is not None:
            self.tell(new, topic_ctx, chat, row)
        return "done", ""

    def tell(self, app, topic_ctx, chat, row) -> None:
        text = (f"**🔁 已换配置**（主人在私聊的批量换配置卡上确认）\n模型：{row['from']['label']} → {row['to']['label']}\n"
                "下一条消息由新会话接手，上下文不断：它拿到交接摘要和最近的问答；更换前的任务卡只能查看。")
        try:
            app.messenger.reply(topic_ctx, chat, text, app.key("batch-switch", f"{row['topic_id'][:16]}-{row['to']['model']}"),
                                voice=app.voice)
        except Exception as exc:  # noqa: BLE001 - the switch is done; the card says so
            self.runtime.log(f"batch switch notice failed topic={row['topic_id'][:12]}: {type(exc).__name__}")
