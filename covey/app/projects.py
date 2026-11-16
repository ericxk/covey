"""专项的升级、直接开专项和降级 (docs/architecture.md 专项, phase 2, COVEY_PROJECTS=1, off by default).

- **Upgrade** (``consider``, called by app/main_topics.py ``dispatch_card`` once a 派发卡 is sent and recorded): a quick job the main thread
  handed to a topic becomes a project when COVEY_PROJECT_UPGRADE says so (``word``: the owner said 长活 or the butler passed
  ``project``; ``auto``: also the topic's third round). Silent: the 派发卡 becomes the project's card (the same message and
  card id, drawn whole by app/project_cards.py), and the card has the 不要专项 button. ``Projects.upgrade`` is idempotent:
  each of its seven steps is ticked off in the record's ``steps`` once done, a run that stopped anywhere goes on from there
  (``advance``; the thread ``Projects.threads`` gives retries what a crash left, with a backoff), and every step does the
  same again harmlessly when its tick was lost.
- **Direct** (``start``, from topic_start with ``project`` true): a record, the lead's topic, the project's card with the
  "你问" block, the start receipt; the lead decides which threads to open.
- **Downgrade** is app/project_cards.py ``downgrade`` (the card's 不要专项 button; it lives there so that module and this one do
  not import each other).
- The butler's two tools ``projects_list`` and ``project_send`` (``TOOLS``, listed to the main thread when COVEY_PROJECTS=1,
  core/main_tool_specs.py): the second hands the owner's words to a project's lead as a turn the owner started.

The receipts go to the lead's inbox through app/project_lead.py ``add_receipt`` (kind ``start``, written here; the lead's
wake-up is the project loop's, not this module's). Locks (rules 1 and 2): none is held across a Feishu call; a topic's lock is
taken before LOCK (``set_topic_project`` takes LOCK inside it) and the card's lock before the record's."""
from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path

from covey.app import dispatch_cards, project_cards, project_lead
from covey.app.project_cards import DOWNGRADED, topic_ctx
from covey.app.main_env import reply
from covey.cards.project_card import merged_card
from covey.core import dispatch_records as drecords
from covey.core import env as cenv
from covey.core import main_thread, main_thread_settings, projects
from covey.core.card_sequence import file_card, reserve
from covey.core.jobs import find_job_record, topic_job_records
from covey.core.locks import LOCK, resource_lock, topic_lock
from covey.core.loops import every
from covey.core.state import read_object, write_json
from covey.core.topics import move_topic_message, route_message, set_topic_project, topic_file, topic_label, topic_title

ENV = "COVEY_PROJECT_UPGRADE"
OFF, WORD, AUTO = "off", "word", "auto"
ROUNDS = 3  # ``auto``: the topic's round that makes a project.
UPGRADE_STEPS = ("record", "lead", "thread", "card", "merged", "route", "receipt")
START_STEPS = ("record", "lead", "card", "receipt")
KINDS = {"upgrade": UPGRADE_STEPS, "direct": START_STEPS}
RESUME_TICK = 30.0  # Seconds between two looks for a project whose steps a crash left unfinished.
RESUME_AFTER = 60.0  # A project written to this recently is still being worked on (by a tool call): left alone.
RESUME_GIVE_UP = 600.0  # The longest wait between two tries of one project.
NOTES = {"word": "由快事升级（你说了长活）", "rounds": "由快事升级（来回第 {} 轮）"}
LEAD_DROPS = ("topic_id", "main_thread", "track", "project", "origin", "root_id", "thread_id", "topic_number", "retired")
NO_CHAT = "还不知道主人和机器人的私聊（主人还没在私聊里发过消息），没有执行。"
OFF_WORDS = "专项没有开（COVEY_PROJECTS）。"


def mode() -> str:
    """COVEY_PROJECT_UPGRADE: ``word`` or ``auto``; anything else, unset too, is ``off`` (read on every call)."""
    value = str(cenv.get(ENV, "")).strip().lower()
    return value if value in (WORD, AUTO) else OFF


def project_id(kind, key) -> str:
    """The id of the project that ``key`` (a 派发卡's token, a topic_start request's token) makes: the same every time,
    so a repeated upgrade or request finds its record."""
    return hashlib.sha256(f"project\0{kind}\0{key}".encode()).hexdigest()[:12]


def rounds_for_topic(state_dir, topic_id) -> int:
    """How many rounds ``topic_id`` has had: the larger of the 派发卡 records that name it and the turns the owner started in
    it himself (a job whose message id is a Feishu message's, ``om_``). The record's own ``rounds`` field (the same question
    dispatched again) is not read."""
    cards = sum(1 for path in drecords.live_paths(state_dir) if read_object(path).get("topic_id") == topic_id)
    asked = sum(1 for job in topic_job_records(Path(state_dir), topic_id) if str(job.get("message_id") or "").startswith("om_"))
    return max(cards, asked)


def clip(text, limit) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def has_receipt(state_dir, pid, kind) -> bool:
    """Whether the lead's inbox already has a receipt of ``kind`` (a step that crashed after writing one is not written twice)."""
    try:
        paths = sorted(project_lead.inbox_dir(state_dir, pid).glob("*.json"))
    except OSError:
        return False
    return any(read_object(path).get("kind") == kind for path in paths)


def state_of(record) -> str:
    return str(record.get("state") or projects.ACTIVE)


_busy: set = set()  # Project ids whose steps this process is working through right now.
_busy_lock = threading.Lock()


class Projects:
    """The Bridge's feature object (app/unified.py PARTS ``projects``): ``threads()`` gives the thread that finishes what a
    crash left half done; none with COVEY_PROJECTS unset. The upgrade and the direct start are called with the same ``env``
    (app/main_env.py MainEnv) the main thread's tools get."""
    name = "projects"

    def __init__(self, env) -> None:
        self.env = env
        self.tries: dict = {}  # project id -> (tries, next try)

    # What runs by itself --------------------------------------------------------------------------------------------

    def threads(self):
        return ((self.name, self.run),) if projects.enabled() else ()

    def run(self, ctx) -> None:
        every(self.env.runtime, RESUME_TICK, self.resume, isolate=self.name)

    def resume(self, now=None) -> None:
        """Go on with every project of ours whose steps are not all done and that has not been worked on for RESUME_AFTER
        seconds; one that keeps failing is tried less and less often."""
        if not projects.enabled() or not self.env.state_dir:
            return
        now = time.time() if now is None else now
        for record in projects.records(self.env.state_dir):
            origin = record.get("origin") if isinstance(record.get("origin"), dict) else {}
            steps = KINDS.get(origin.get("kind"))
            if steps is None or state_of(record) != projects.ACTIVE or all(name in (record.get("steps") or {}) for name in steps):
                self.tries.pop(record.get("id"), None)
                continue
            tries, later = self.tries.get(record["id"], (0, 0.0))
            if now < later or now - float(record.get("updated_at") or 0) < RESUME_AFTER:
                continue
            if self.advance(record["id"]):
                self.tries.pop(record["id"], None)
            else:
                self.tries[record["id"]] = (tries + 1, now + min(RESUME_GIVE_UP, RESUME_TICK * 2 ** (tries + 1)))

    # Upgrade ---------------------------------------------------------------------------------------------------------

    def consider(self, dispatch) -> str:
        """The id of the project the 派发卡 ``dispatch`` (its record, or its token: core/dispatch_records.py) became, '' when it
        did not. Never raises (the card stays a quick job's). Does nothing with COVEY_PROJECTS or COVEY_PROJECT_UPGRADE off."""
        if not projects.enabled() or mode() == OFF:
            return ""
        try:
            return self._consider(dispatch)
        except Exception as exc:  # noqa: BLE001
            topic = dispatch.get("topic_id") if isinstance(dispatch, dict) else ""
            self.env.runtime.log(f"project upgrade failed topic={str(topic)[:12]}: {type(exc).__name__}")
            return ""

    def _consider(self, dispatch) -> str:
        state = Path(self.env.state_dir)
        if isinstance(dispatch, str):
            dispatch = read_object(drecords.record_path(state, dispatch))
        if projects.topic_project(state, str(dispatch.get("topic_id") or "")) is not None:
            return ""  # A project's thread, or a lead: not a quick job.
        why = self.reason(dispatch)
        return self.upgrade(dispatch, *why) if why else ""

    def reason(self, dispatch):
        """(why, rounds) when COVEY_PROJECT_UPGRADE makes ``dispatch`` a project, else None. ``word``: the owner's own word
        (upgrade_hint.long_word) or the butler's ``project`` (upgrade_hint.project); ``auto``: also the third round."""
        if mode() == OFF or dispatch.get("state") != drecords.LIVE:
            return None
        hint = dispatch.get("upgrade_hint") if isinstance(dispatch.get("upgrade_hint"), dict) else {}
        if hint.get("project"):
            return "word", 0  # The butler says it: the owner's own, new word.
        if self.turned_back(dispatch.get("topic_id")):
            return None  # 不要专项 was his word on this topic: a long word or a third round does not overrule it.
        if hint.get("long_word"):
            return "word", 0
        if mode() == AUTO:
            rounds = rounds_for_topic(self.env.state_dir, str(dispatch.get("topic_id") or ""))
            if rounds >= ROUNDS:
                return "rounds", rounds
        return None

    def turned_back(self, topic) -> bool:
        """Whether the owner has already turned a project that grew from ``topic`` back (不要专项)."""
        return any(record.get("state_reason") == DOWNGRADED and (record.get("origin") or {}).get("topic_id") == topic
                   for record in projects.records(self.env.state_dir) if record.get("state") == projects.CANCELLED)

    def upgrade(self, dispatch, why="word", rounds=0) -> str:
        """Turn the 派发卡 ``dispatch`` and its topic into a project (the seven ``UPGRADE_STEPS``); the project's id, '' when
        there is none (``MAX_ACTIVE`` projects are on, or the card was not sent). Run again after a crash it goes on from
        the first step not done."""
        state, token = Path(self.env.state_dir), str(dispatch.get("token") or "")
        topic = str(dispatch.get("topic_id") or "")
        if not (token and topic and dispatch.get("card_id") and dispatch.get("card_message_id")):
            return ""
        pid = project_id("upgrade", token)
        if not projects.read_record(state, pid):
            if projects.active_count(state) >= projects.MAX_ACTIVE:
                self.env.runtime.log(f"project upgrade skipped: {projects.MAX_ACTIVE} projects are on topic={topic[:12]}")
                return ""
            note = NOTES["word"] if why == "word" else NOTES["rounds"].format(rounds)
            made = projects.new_record(
                chat_id=dispatch.get("chat_id"), name=topic_title(state, topic) or clip(dispatch.get("request"), 30) or "专项",
                goal=str(dispatch.get("request") or ""), project_id=pid,
                origin={"kind": "upgrade", "token": token, "topic_id": topic, "why": why, "rounds": rounds, "note": note})
            made["steps"] = {"record": round(time.time(), 3)}
            made["ask"] = {key: value for key, value in (("text", dispatch.get("request")), ("at", dispatch.get("ask_at")),
                                                         ("url", dispatch.get("ask_url"))) if value}
            projects.create(state, made)
        self.advance(pid)
        return pid

    def advance(self, pid) -> bool:
        """Do the steps of project ``pid`` that are not ticked off in its record, in order, ticking each one when it is
        done; stops at the first that raises or is not finished. True when every step is done. A project another thread
        of this process is working on, one that is not on any more, and one nobody here made, is left alone (False)."""
        with _busy_lock:
            if pid in _busy:
                return False
            _busy.add(pid)
        try:
            return self._advance(pid)
        finally:
            with _busy_lock:
                _busy.discard(pid)

    def _advance(self, pid) -> bool:
        state, log = Path(self.env.state_dir), self.env.runtime.log
        record = projects.read_record(state, pid)
        origin = record.get("origin") if isinstance(record.get("origin"), dict) else {}
        steps = KINDS.get(origin.get("kind"))
        if steps is None or state_of(record) != projects.ACTIVE:
            return False
        for name in steps:
            if name in (record.get("steps") or {}):
                continue
            try:
                done = getattr(self, "step_" + name)(record)
            except Exception as exc:  # noqa: BLE001 - a later run goes on from here
                log(f"project step failed project={pid[:8]} step={name}: {type(exc).__name__}")
                return False
            if not done:
                log(f"project step not done project={pid[:8]} step={name}")
                return False

            def tick(data, name=name):
                done_steps = data.get("steps") if isinstance(data.get("steps"), dict) else {}
                done_steps[name] = round(time.time(), 3)
                data["steps"] = done_steps
            record = projects.update(state, pid, tick)
        return True

    def step_record(self, record) -> bool:
        return True  # Written when the project was made; the tick is part of that write.

    def step_lead(self, record) -> bool:
        """The lead's topic: topic.json with ``project`` {id, role lead}, the main thread's own model settings (Claude Code,
        its model and budget: the butler's), the workspace of the topic the project grew from (else the one the direct start
        chose, else the main thread's), locked."""
        state, chat, pid = Path(self.env.state_dir), str(record["chat_id"]), record["id"]
        lead, origin = record["lead_topic_id"], record.get("origin") or {}
        field = projects.project_field(pid, projects.LEAD)
        path = topic_file(state, lead)
        if read_object(path).get("project") == field:
            return True
        root = read_object(topic_file(state, main_thread.main_topic_id(chat)))
        if not root.get("harness_id"):
            raise RuntimeError("the main thread has no topic.json yet")
        source = read_object(topic_file(state, origin["topic_id"])) if origin.get("topic_id") else {}
        data = {key: value for key, value in root.items() if key not in LEAD_DROPS}
        data.update(topic_id=lead, chat_id=chat, root_id="", thread_id="", binding_revision=1, project=field,
                    workspace_locked=True, harness_locked=True, model_settings_snapshot=True,
                    workspace_name=source.get("workspace_name") or origin.get("workspace") or root.get("workspace_name") or "",
                    workdir=source.get("workdir") or origin.get("workdir") or root.get("workdir") or "")
        with LOCK:
            write_json(path, {**read_object(path), **data}, ensure_ascii=False, indent=2)
        return True

    def step_thread(self, record) -> bool:
        """The topic the project grew from is its first thread: ``project`` {id, role thread} in its topic.json, under its
        topic lock, and in the record's ``threads``."""
        state, topic = Path(self.env.state_dir), record["origin"]["topic_id"]
        with topic_lock(topic_ctx(state, topic)):
            set_topic_project(state, topic, record["id"], projects.THREAD)
        projects.add_thread(state, record["id"], topic)
        return True

    def step_card(self, record) -> bool:
        """The project's card. An upgrade takes the 派发卡's own (the same card id and message): under the card's lock the
        dispatch record is settled first (``DispatchCards`` writes it no more) and the sequence is handed on, then the
        project adopts the card. A card that cannot be taken (no card, or the dispatch record ended some other way) or a
        project opened directly sends its own."""
        state, origin = Path(self.env.state_dir), record.get("origin") or {}
        if origin.get("kind") != "upgrade":
            return bool(project_cards.created(self.env, record))
        path = drecords.record_path(state, origin["token"])
        dispatch = read_object(path)
        card_id, message = str(dispatch.get("card_id") or ""), str(dispatch.get("card_message_id") or "")
        if not (card_id and message):
            return bool(project_cards.created(self.env, record))
        with resource_lock("card", card_id):
            with resource_lock("record", str(path)):
                saved = read_object(path)
                if saved.get("state") == drecords.LIVE:
                    drecords.transition(saved, drecords.SETTLED)
                    saved.update(settled_at=time.time(), settled_by="upgrade", project_id=record["id"])
                    write_json(path, saved, ensure_ascii=False, indent=1)
                taken = saved.get("project_id") == record["id"]
            if taken:
                if projects.card_message(projects.read_record(state, record["id"])) != message:  # Not adopted twice: the
                    project_cards.adopt_card(  # sequence would go back to the dispatch record's.
                        state, record["id"], card_id=card_id, message_id=message, sequence=int(saved.get("sequence") or 0),
                        created_at=saved.get("started_at"))
                return True
        return bool(project_cards.created(self.env, record))

    def others(self, record) -> list:
        """The other 派发卡 of the topic the project grew from that are still live: (path, record)."""
        state, topic = Path(self.env.state_dir), (record.get("origin") or {}).get("topic_id")
        found = []
        for path in drecords.live_paths(state):
            saved = read_object(path)
            if (saved.get("topic_id") == topic and saved.get("state") == drecords.LIVE and saved.get("card_id")
                    and saved.get("card_message_id") and saved.get("token") != (record.get("origin") or {}).get("token")):
                found.append((path, saved))
        return found

    def step_merged(self, record) -> bool:
        """Each other live 派发卡 of the topic becomes one line, "已并入专项 ›", and its record is settled. A write CardKit
        did not confirm leaves the card live and the step not done."""
        found = self.others(record)
        if not found:
            return True
        url = self.card_url(record)
        done = True
        for path, saved in found:
            done = self.merge(path, saved, record, url) and done
        return done

    def merge(self, path, saved, record, url) -> bool:
        env = self.env
        handle = file_card(path, saved, shared=False)
        with handle.lock:
            if read_object(path).get("state") != drecords.LIVE:
                return True  # Settled while this waited.
            sequence = reserve(handle)
            built = merged_card(record.get("name"), url)
            result = env.messenger.put_card(env.profile, saved["card_id"], built, sequence=sequence,
                                            key=f"project-merged-{str(saved.get('token'))[:20]}-{sequence}", timeout=3)
        if result is None:
            env.runtime.log(f"project merged line not written token={str(saved.get('token'))[:8]}")
            return False
        with resource_lock("record", str(path)):
            now = read_object(path)
            if now.get("state") == drecords.LIVE:
                drecords.transition(now, drecords.SETTLED)
                now.update(settled_at=time.time(), settled_by="merged", project_id=record["id"])
                write_json(path, now, ensure_ascii=False, indent=1)
        return True

    def card_url(self, record) -> str:
        """The link that opens the project's card at its place in the chat; '' when it cannot be read (the line has none)."""
        from covey.app.topic_links import message_link, place_of

        message = projects.card_message(projects.read_record(self.env.state_dir, record["id"]))
        try:
            where = place_of(self.env.state_dir, record["lead_topic_id"], message, self.env.profile) if message else {}
        except Exception:  # noqa: BLE001 - the line without its link
            return ""
        return message_link(record.get("chat_id"), where["position"]) if where.get("position") else ""

    def step_route(self, record) -> bool:
        """A quote of the project's card, and of each line it took in, goes to the lead's topic (``route_message``; the
        message stays the topic's it was)."""
        state, pid, chat = Path(self.env.state_dir), record["id"], str(record["chat_id"])
        topic = (record.get("origin") or {}).get("topic_id") or record["lead_topic_id"]
        messages = {projects.card_message(projects.read_record(state, pid))}
        for path in drecords.live_paths(state):
            saved = read_object(path)
            if saved.get("project_id") == pid:
                messages.add(str(saved.get("card_message_id") or ""))
        card = projects.card_message(projects.read_record(state, pid))
        for message in sorted(messages - {""}):
            if message == card:
                # The card's clicks too: its buttons name the lead's topic (card_routing.py refuses a click routed to
                # another topic), and the 派发卡 it was belonged to the topic that is now a thread (accept7, 2026-11-16).
                move_topic_message(state, record["lead_topic_id"], chat, message)
            route_message(state, chat, message, record["lead_topic_id"], holder=topic)
        return True

    def step_receipt(self, record) -> bool:
        """The start receipt in the lead's inbox: an upgrade says what the lead takes over, a direct start has the owner's
        request as he wrote it. Once, whatever happened to the step before."""
        state, pid, origin = Path(self.env.state_dir), record["id"], record.get("origin") or {}
        if not has_receipt(state, pid, "start"):
            project_lead.add_receipt(state, pid, "start", self.handover(state, record) if origin.get("kind") == "upgrade"
                                     else origin.get("text") or record.get("goal") or record.get("name"),
                                     thread=origin.get("topic_id") or "")
        return True

    @staticmethod
    def handover(state, record) -> str:
        """What the lead is told it takes over: the topic, what the owner asked for, how it stands."""
        topic = record["origin"]["topic_id"]
        label, title = topic_label(state, topic), topic_title(state, topic)
        parts = ["接手一个已在进行的事：话题 " + " ".join(part for part in (label, f"「{clip(title, 30)}」" if title else "") if part)]
        if record.get("goal"):
            parts.append("主人要的：" + clip(record["goal"], 160))
        job = project_cards.newest_job(state, topic) or {}
        if job:
            parts.append("现在：" + clip(" ".join(part for part in (str(job.get("status") or ""), str(job.get("status_message") or "")) if part), 120))
        return "；".join(parts)

    # Direct ----------------------------------------------------------------------------------------------------------

    def start(self, request, args) -> dict:
        """topic_start with ``project`` true: the project's record, the lead's topic, the project's card (the owner's message
        above it) and the start receipt carrying his words; the lead then decides which threads to open. Once a request
        (rule 6 of app/main_topics.py: its answer is kept in the request's record)."""
        from covey.app import main_handoff, main_topics

        env, state = self.env, Path(self.env.state_dir)
        chat = env.chat()
        path = main_topics.record_path(state, request)
        saved = read_object(path)
        if saved.get("answer"):
            return saved["answer"]
        pid = project_id("direct", main_topics.request_token(request))
        record = projects.read_record(state, pid)
        if not record:
            if projects.active_count(state) >= projects.MAX_ACTIVE:
                return reply(False, f"已经有 {projects.MAX_ACTIVE} 个专项在做，不再开新的：先让主人收尾或停掉一个，"
                                    "或把这件事当普通话题开（topic_start 不带 project）。")
            plan = main_topics.plan_start(env, args, main_thread_settings.load(state))
            if isinstance(plan, str):
                return reply(False, plan)
            title, prompt = main_topics._text(args.get("title"), 40), main_topics._text(args.get("prompt"))
            words = main_handoff.asked(state, request.job_key).words
            made = projects.new_record(
                chat_id=chat, name=title, goal=prompt, project_id=pid,
                origin={"kind": "direct", "request": request.id, "job_key": request.job_key, "workspace": plan["workspace"],
                        "workdir": plan["workdir"], "why": main_topics._text(args.get("why"), 200), "text": words or prompt})
            made["steps"] = {"record": round(time.time(), 3)}
            ask = dispatch_cards.ask_of(env, state, request.job_key)
            asked = dispatch_cards.request_words(state, request.job_key)
            made["ask"] = {key: value for key, value in (("text", asked), ("at", ask.get("ask_at")), ("url", ask.get("ask_url")))
                           if value}
            record, _ = projects.create(state, made)
        done = self.advance(pid)
        record = projects.read_record(state, pid) or record
        sent = bool(projects.card_message(record))
        note = {"request": request.id, "job_key": request.job_key, "chat_id": chat, "project_id": pid, "at": time.time()}
        text = (f"已开专项「{record.get('name')}」（{pid}）：负责人会派线程去做，专项卡在聊天区，进展看卡；不用等它做完。"
                if done else f"专项「{record.get('name')}」已建（{pid}），"
                             f"{'' if sent else '专项卡还没发出去，'}有一步没做完，Bridge 会自己补；不要再开一次。")
        return main_topics._done(path, note, reply(True, text, project_id=pid, lead_topic_id=record.get("lead_topic_id"),
                                                   card=sent, finished=done))


# The butler's tools ----------------------------------------------------------------------------------------------------

STATE_WORDS = {projects.ACTIVE: "在做", projects.PAUSED: "停住", projects.DONE: "做完", projects.CANCELLED: "已取消"}
LISTED_ENDED = 3  # Projects that ended, listed after the ones that are on.


def butler_only(env, request):
    """A refusal (the answer), or None when the call is the main thread's: the lead has its own tools."""
    from covey.app import main_topics

    if not projects.enabled():
        return reply(False, OFF_WORDS)
    if main_topics.lead_of(env, request) is not None:
        return reply(False, "负责人不用这个工具：它只管自己的专项，线程用 project_thread_start、project_thread_send。")
    return None


def projects_list(env, request):
    refused = butler_only(env, request)
    if refused is not None:
        return refused
    state, chat = Path(env.state_dir), env.chat()
    found = projects.records(state, chat)
    live = [record for record in found if state_of(record) in (projects.ACTIVE, projects.PAUSED)]
    ended = [record for record in found if record not in live][-LISTED_ENDED:]
    if not live and not ended:
        return reply(True, "还没有专项。", count=0)
    rows = []
    for record in [*live, *ended]:
        threads = "、".join(topic_label(state, topic) or topic[:6] for topic in record.get("threads") or ()) or "还没有线程"
        note = project_cards.lead_note(record)
        rows.append(f"{record['id']} · {clip(record.get('name'), 30)} · {STATE_WORDS.get(state_of(record), state_of(record))}"
                    f" · 线程 {threads}" + (f" · 最近：{clip(note, 40)}" if note else ""))
    return reply(True, "\n".join(rows), count=len(found),
                 projects=[{"id": record["id"], "name": record.get("name"), "state": state_of(record)}
                           for record in [*live, *ended]])


def find_project(state, chat, ref):
    """The record ``ref`` names (its id, an id's start, or its name when only one has it) among the chat's that are on;
    a str saying why not."""
    text = " ".join(str(ref or "").split())
    if not text:
        return "pid 不能为空：用 projects_list 查专项的 id。"
    found = [record for record in projects.records(state, chat) if state_of(record) in (projects.ACTIVE, projects.PAUSED)]
    for same in (lambda record: record["id"] == text, lambda record: record["id"].startswith(text),
                 lambda record: text in str(record.get("name") or "")):
        matches = [record for record in found if same(record)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            return f"「{text}」对得上不止一个专项：{'、'.join(record['id'] for record in matches)}，给完整的 id。"
    return f"没有在做的专项「{text}」：用 projects_list 查 id。"


def lead_target(env, record):
    """(app, context) of the project's lead topic, or None when its topic.json cannot be read or its Harness has no app here.
    ``Topics.target`` gives none for a lead (its Traits row is not a card target), and the lead's topic.json has no root
    message to address it by: the context comes from an alias of the Bridge's own, ``project-lead-<id>``, never a Feishu
    message's id, that belongs to the lead (the way app/main_tracks.py ``enter`` gets a track's)."""
    from covey.core.topics import load_topic

    state, chat, lead = Path(env.state_dir), str(record.get("chat_id") or ""), str(record.get("lead_topic_id") or "")
    try:
        topic = load_topic(state, lead)
    except (OSError, ValueError):
        return None
    app = env.topics.apps.get(topic.get("harness_id"))
    if app is None or topic.get("chat_id") != chat:
        return None
    alias = projects.LEAD_ALIAS_PREFIX + str(record["id"])
    move_topic_message(state, lead, chat, alias)
    return app, app.submission.event_context(app.harness.context, {"chat_id": chat, "message_id": alias})


def lead_prompt(name, text, owner) -> str:
    """What the lead's model gets when the butler hands it the owner's words (the Bridge's note, the butler's task text, the
    owner's own words quoted): a turn the owner started, not a wake-up."""
    from covey.app.main_handoff import NOTE

    parts = [f"{NOTE} 主人在私聊里对专项「{name}」说了话，由主线程管家转给你；这是主人触发的一轮，不是自动唤醒，你的回复会由 Bridge "
             "发给主人。按他的意思调整专项：派线程、给线程加话、记决定或改风格就用 project_* 工具。",
             "管家整理的说明：\n" + text]
    if owner.words:
        parts.append("主人原话：\n" + "\n".join("> " + line for line in owner.words.split("\n")))
    return "\n\n".join(parts)


def project_send(env, request):
    """Hand the owner's words to a project's lead: a supplement to the turn it is in, else its next turn (message id
    "main-<token>": the owner's, not a wake-up). Once a request."""
    from covey.app import main_handoff, main_topics
    from covey.core.history_storage import append_history
    from covey.core.steer import SteerRefused, SteerUncertain

    refused = butler_only(env, request)
    if refused is not None:
        return refused
    chat, state = env.chat(), Path(env.state_dir)
    if not chat:
        return reply(False, NO_CHAT)
    text = main_topics._text(request.args.get("text"))
    if not text:
        return reply(False, "text 不能为空：写这次交给负责人的事。")
    path, token = main_topics.record_path(state, request), main_topics.request_token(request)
    saved = read_object(path)
    if saved.get("answer"):
        return saved["answer"]
    record = find_project(state, chat, request.args.get("pid"))
    if isinstance(record, str):
        return reply(False, record)
    name, lead, mid = clip(record.get("name"), 30) or "专项", record["lead_topic_id"], "main-" + token
    found = lead_target(env, record)
    if found is None:
        return reply(False, f"专项「{name}」的负责人话题读不到，这次没有转。")
    app, ctx = found
    # No ``topic_id``: the readers of main-thread/opened (core/main_opened.py) take a record with one for a topic the request handed
    # on to, and the lead is not one (it is not relayed, has no T number and no thread of its own).
    note = {"request": request.id, "job_key": request.job_key, "chat_id": chat, "lead_topic_id": lead, "project_id": record["id"],
            "at": time.time()}
    main_topics._save(path, note)
    owner = main_handoff.asked(state, request.job_key)
    sent = lead_prompt(name, text, owner)
    shown = owner.words or text
    data = {"project_id": record["id"], "lead_topic_id": lead}
    job = main_topics.live_turn(app, ctx, chat)
    if job is not None:
        try:
            app.steering.deliver_steer(ctx, job, sent, mid, shown=shown)
        except SteerUncertain as exc:
            return main_topics._done(path, note, reply(False, f"补充可能已送到专项「{name}」负责人的任务 #{job.job_id}，送达没有确认（{exc}）；"
                                                      "不会重发，别再转一次。", **data, uncertain=True))
        except SteerRefused as exc:  # Nothing was sent (rule 6).
            return main_topics._done(path, note, reply(False, f"没有送进专项「{name}」负责人正在跑的任务 #{job.job_id}：{exc}。没有发出；"
                                                      "等这一轮结束后再转。", **data))
        return main_topics._done(path, note, reply(True, f"已转给专项「{name}」的负责人：作为补充送入它正在跑的任务 #{job.job_id}。",
                                                  **data, steered=True))
    if not (find_job_record(state, mid) or app.runtime.jobs.settling(mid)):
        append_history(ctx.history_path, "user", shown, {"message_id": mid})
        if app.submission.enqueue_chat_job(ctx, shown, chat, mid, prompt_content=sent, title=name) is None:
            return main_topics._done(path, note, reply(False, f"专项「{name}」负责人的下一轮没有提交：模型配置无法确认。", **data))
    return main_topics._done(path, note, reply(True, f"已转给专项「{name}」的负责人：它开了新的一轮，结果会由 Bridge 发给主人。",
                                              **data, steered=False))


TOOLS = {"projects_list": projects_list, "project_send": project_send}
