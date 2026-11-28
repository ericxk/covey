"""专项的升级、直接开专项和降级 (docs/architecture.md 专项, phase 2, COVEY_PROJECTS=1, off by default).

- **Upgrade** (``consider``, called by app/butler_topics.py ``dispatch_card`` once a 派发卡 is sent and recorded): a quick job the butler
  handed to a topic becomes a project when COVEY_PROJECT_UPGRADE says so (``butler``: the butler passed
  ``project``; ``auto``: also the topic's third round). Silent: the 派发卡 becomes the project's card (the same message and
  card id, drawn whole by app/project_cards.py), and the card has the 不要专项 button. ``Projects.upgrade`` is idempotent:
  each of its seven steps is ticked off in the record's ``steps`` once done, a run that stopped anywhere goes on from there
  (``advance``; the thread ``Projects.threads`` gives retries what a crash left, with a backoff), and every step does the
  same again harmlessly when its tick was lost.
- **Direct** (``start``, from topic_start with ``project`` true): a record, the lead's topic, the project's card with the
  "你问" block, the start receipt; the lead decides which member topics to open. The existing topics the butler names in
  ``topics`` (the owner said to take them in) become members before the receipt (JOIN_STEPS); one that cannot (not found, a
  lead's, another project's) is named in the answer and the receipt, never dropped silently.
- **Downgrade** is app/project_clicks.py ``downgrade`` (the card's 不要专项 button; it lives there so that module and this one do
  not import each other).
- **Gathered** (``gather``, 整理专项's 建专项, app/project_tidy.py): a project of topics that are there already (GATHER_STEPS: the
  record, the lead, the card, each topic a member, the start receipt naming them).
- **Joined and left** (``join_topic``, ``leave_topic``, 整理专项) are app/project_members.py; the butler's tools ``projects_list``,
  ``project_send`` and ``projects_tidy`` are app/butler_projects.py. This module is the flow of making a project.

The receipts go to the lead's inbox through app/project_lead.py ``add_receipt`` (kind ``start``, written here; the lead's
wake-up is the project loop's, not this module's). Locks (rules 1 and 2): none is held across a Feishu call; a topic's lock is
taken before LOCK (``set_topic_project`` takes LOCK inside it) and the card's lock before the record's."""
from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path

from covey.app import (butler_harness, dispatch_cards, lead_harness, project_cards, project_facts, project_lead, project_loop,
                       relay_targets, revisits)
from covey.app.project_clicks import topic_ctx
from covey.app.project_members import GATHERED_NOTE, topic_line, wanted_topics
from covey.app.butler_env import arg_text, reply
from covey.cards.project_card import merged_card
from covey.core import dispatch_records as drecords
from covey.core import env as cenv
from covey.core import butler, butler_opened, butler_settings, devices, projects
from covey.core.card_sequence import file_card, reserve
from covey.core import turn_source
from covey.core.jobs import find_job_record, topic_job_records
from covey.core.locks import LOCK, resource_lock, topic_lock
from covey.core.loops import every
from covey.core.state import read_object, write_json
from covey.core.text import cut
from covey.core.topics import mark_home, move_topic_message, route_message, set_topic_project, topic_file, topic_label, topic_title

ENV = "COVEY_PROJECT_UPGRADE"
OFF, BUTLER, AUTO = "off", "butler", "auto"
WORD = "word"  # The value the live deployments set (and the reason an older record carries): the same as ``butler``.
ROUNDS = 3  # ``auto``: the topic's round that makes a project.
OPENINGS = ("start", "fork")  # The kinds of the 派发卡 records an older release sent when the butler opened a topic.
UPGRADE_STEPS = ("record", "lead", "thread", "card", "merged", "route", "receipt")
START_STEPS = ("record", "lead", "card", "receipt")
GATHER_STEPS = ("record", "lead", "card", "members", "receipt")  # 整理专项's 建专项: topics that are there already.
JOIN_STEPS = ("record", "lead", "card", "members", "receipt")  # A direct start that takes in existing topics (``topics``).
KINDS = {"upgrade": UPGRADE_STEPS, "direct": START_STEPS, "tidy": GATHER_STEPS}
RESUME_TICK = 30.0  # Seconds between two looks for a project whose steps a crash left unfinished.
RESUME_AFTER = 60.0  # A project written to this recently is still being worked on (by a tool call): left alone.
RESUME_GIVE_UP = 600.0  # The longest wait between two tries of one project.
NOTES = {"butler": "由管家转成专项", "rounds": "来回 {} 轮后转成专项"}
LEAD_DROPS = ("topic_id", "main_thread", "track", "project", "origin", "root_id", "thread_id", "topic_number", "retired")


def mode() -> str:
    """COVEY_PROJECT_UPGRADE: ``butler`` (unset or empty too; ``word``, the old name, means the same) or ``auto``; anything
    else is ``off`` (read on every call)."""
    value = str(cenv.get(ENV, "") or "").strip().lower() or BUTLER
    return BUTLER if value in (BUTLER, WORD) else AUTO if value == AUTO else OFF


def project_id(kind, key) -> str:
    """The id of the project that ``key`` (a 派发卡's token, a topic_start request's token) makes: the same every time,
    so a repeated upgrade or request finds its record."""
    return hashlib.sha256(f"project\0{kind}\0{key}".encode()).hexdigest()[:12]


def rounds_for_topic(state_dir, topic_id) -> int:
    """How many rounds ``topic_id`` has had: the larger of the butler's hand-ons and the turns the owner started in it
    himself (a job whose message id is a Feishu message's, ``om_``). The butler's are the 派发卡 records that name it (each
    topic_send's) and its opening, when the butler opened it (topic.json ``origin`` kind ``main``): topic_start and
    topic_fork leave no 派发卡, a record of kind start or fork is one an older release sent for that opening (counted
    once). The record's own ``rounds`` field (the same question dispatched again) is not read."""
    named = [saved for saved in map(read_object, drecords.live_paths(state_dir)) if saved.get("topic_id") == topic_id]
    origin = read_object(topic_file(Path(state_dir), topic_id)).get("origin")
    opened = (isinstance(origin, dict) and origin.get("kind") == "main"
              and not any(saved.get("kind") in OPENINGS for saved in named))
    asked = sum(1 for job in topic_job_records(Path(state_dir), topic_id) if str(job.get("message_id") or "").startswith("om_"))
    return max(len(named) + opened, asked)


def has_receipt(state_dir, pid, kind) -> bool:
    """Whether the lead's inbox already has a receipt of ``kind`` (a step that crashed after writing one is not written twice)."""
    try:
        paths = sorted(project_lead.inbox_dir(state_dir, pid).glob("*.json"))
    except OSError:
        return False
    return any(read_object(path).get("kind") == kind for path in paths)


def owner_message(state, record) -> str:
    """The owner's own message (core/turn_source.py DIRECT: Feishu's, the web page's) that ``record``'s project came of: for
    a direct start, the one the butler's turn that started it answers; for an upgrade, the one its 派发卡 answered
    (core/dispatch_records.py ``owner_message``); either only when that butler turn's words are the owner's
    (turn_source.level). '' for a project 整理专项 made (no word of his began it) and when none is known. Never raises."""
    origin = record.get("origin") if isinstance(record.get("origin"), dict) else {}
    try:
        if origin.get("kind") == "direct":
            key, given = str(origin.get("job_key") or ""), ""
        elif origin.get("kind") == "upgrade":
            dispatch = read_object(drecords.record_path(state, str(origin.get("token") or "")))
            key, given = str(dispatch.get("job_key") or ""), str(dispatch.get("owner_message") or "")
        else:
            return ""
        job = find_job_record(Path(state), key) or {} if key else {}
        if not job or turn_source.level(job) != turn_source.LEVEL_OWNER:
            return ""  # The butler's turn was not the owner's words (a topic's ask, a hand-on), or it is gone.
        found = given or relay_targets.butler_job_owner_message(state, key) or str(job.get("message_id") or "")
    except Exception:  # noqa: BLE001 - the start receipt goes without it: the first turn is a wake-up as before
        return ""
    return found if found.startswith(turn_source.DIRECT) else ""


def steps_of(record):
    """The steps of ``record``'s kind (KINDS), None for a record none of ours made; a direct start with ``topics`` has JOIN_STEPS
    (one without has START_STEPS, so the records of a release before them are as finished as they were)."""
    origin = record.get("origin") if isinstance(record.get("origin"), dict) else {}
    if origin.get("kind") == "direct" and origin.get("topics"):
        return JOIN_STEPS
    return KINDS.get(origin.get("kind"))


_busy: set = set()  # Project ids whose steps this process is working through right now.
_busy_lock = threading.Lock()


def lead_wanted(env, args, plan):
    """What topic_start with ``project`` true named for the project's lead: {harness, model, effort} as ``plan``
    (app/butler_topics.py plan_start) resolved them when the call named a ``harness`` or ``model``, {} when it named neither
    (the lead then takes the settings' 专项负责人, else the butler's: app/lead_harness.py binding). A str saying why not when
    that Harness cannot run a lead here."""
    if not (arg_text(args.get("harness")) or arg_text(args.get("model"))):
        return {}
    why = lead_harness.refusal(env.topics.apps, plan["harness"])
    if why:
        return (f"{why}；这次没开专项。harness、model 在开专项时是负责人用的：负责人照默认就不填；"
                "主人要专项的话题用哪个模型，写进 prompt，负责人开话题时照办。")
    return {"harness": plan["harness"], "model": plan["model"], "effort": plan["effort"]}


class Projects:
    """The Bridge's feature object (app/unified.py PARTS ``projects``): ``threads()`` gives the thread that finishes what a
    crash left half done; none with COVEY_PROJECTS unset. The upgrade and the direct start are called with the same ``env``
    (app/butler_env.py ButlerEnv) the butler's tools get."""
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
            steps = steps_of(record)
            if steps is None or projects.state_of(record) != projects.ACTIVE or all(name in (record.get("steps") or {}) for name in steps):
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
            return ""  # A project's member, or a lead: not a quick job.
        why = self.reason(dispatch)
        return self.upgrade(dispatch, *why) if why else ""

    def reason(self, dispatch):
        """(why, rounds) when COVEY_PROJECT_UPGRADE makes ``dispatch`` a project, else None. ``butler``: only the butler's
        ``project`` (upgrade_hint.project); no word of the owner's is matched any more; ``auto``: also the third round."""
        if mode() == OFF or dispatch.get("state") != drecords.LIVE:
            return None
        hint = dispatch.get("upgrade_hint") if isinstance(dispatch.get("upgrade_hint"), dict) else {}
        if hint.get("project"):
            return BUTLER, 0  # The butler judged it: the owner wants it followed for days, or said 开个专项 himself.
        if self.turned_back(dispatch.get("topic_id")):
            return None  # 不要专项 was his word on this topic: a third round does not overrule it.
        if mode() == AUTO:
            rounds = rounds_for_topic(self.env.state_dir, str(dispatch.get("topic_id") or ""))
            if rounds >= ROUNDS:
                return "rounds", rounds
        return None

    def turned_back(self, topic) -> bool:
        """Whether the owner has already turned a project that grew from ``topic`` back (不要专项)."""
        return any(record.get("state_reason") == projects.DOWNGRADED and (record.get("origin") or {}).get("topic_id") == topic
                   for record in projects.records(self.env.state_dir) if record.get("state") == projects.CANCELLED)

    def upgrade(self, dispatch, why=BUTLER, rounds=0) -> str:
        """Turn the 派发卡 ``dispatch`` and its topic into a project (the seven ``UPGRADE_STEPS``); the project's id, '' when
        there is none (``MAX_ACTIVE`` projects are on and none could be folded away for it, app/project_loop.py make_room,
        or the card was not sent). Run again after a crash it goes on from the first step not done."""
        state, token = Path(self.env.state_dir), str(dispatch.get("token") or "")
        topic = str(dispatch.get("topic_id") or "")
        if not (token and topic and dispatch.get("card_id") and dispatch.get("card_message_id")):
            return ""
        pid = project_id("upgrade", token)
        if not projects.read_record(state, pid):
            if not project_loop.make_room(state, self.env.runtime.log).ok:
                self.env.runtime.log(f"project upgrade skipped: {projects.MAX_ACTIVE} projects are on topic={topic[:12]}")
                return ""
            note = NOTES["rounds"].format(rounds) if why == "rounds" else NOTES[BUTLER]
            made = projects.new_record(
                chat_id=dispatch.get("chat_id"), name=topic_title(state, topic) or cut(dispatch.get("request"), 30) or "专项",
                goal=str(dispatch.get("request") or ""), project_id=pid,
                origin={"kind": "upgrade", "token": token, "topic_id": topic, "why": why, "rounds": rounds, "note": note})
            made["steps"] = {"record": round(time.time(), 3)}
            made["ask"] = {key: value for key, value in (("text", dispatch.get("request")), ("at", dispatch.get("ask_at")),
                                                         ("url", dispatch.get("ask_url"))) if value}
            self.seed_style(made)
            projects.create(state, made)
        self.advance(pid)
        return pid

    def seed_style(self, record) -> None:
        """The new project's record starts with the style last set on any project (the file is app/project_lead.py
        ``last_style``), none before one was set."""
        style = project_lead.last_style(self.env.state_dir)
        if style:
            record["style"] = style

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
        steps = steps_of(record)
        if steps is None or projects.state_of(record) != projects.ACTIVE:
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
        """The lead's topic: topic.json with ``project`` {id, role lead}; its Harness, model and budget as app/lead_harness.py
        ``binding`` picks them (what the butler's call named for it, else the settings' 专项负责人, else the butler's own, from
        the butler's topic.json or as its first message would write them); the workspace of the topic the
        project grew from (else the one the direct start chose, the home, else the butler's), locked, and the repositories
        of both, read."""
        state, chat, pid = Path(self.env.state_dir), str(record["chat_id"]), record["id"]
        lead, origin = record["lead_topic_id"], record.get("origin") or {}
        field = projects.project_field(pid, projects.LEAD)
        path = topic_file(state, lead)
        if read_object(path).get("project") == field:
            return True
        root = read_object(topic_file(state, butler.butler_topic_id(chat)))
        if not root.get("harness_id"):
            # The butler has not run in this chat yet (a project 整理专项 or the web page started): what its first message
            # would write, on the Harness it runs on now (app/butler_harness.py unwritten_topic).
            root = butler_harness.unwritten_topic(state, self.env.topics.apps)
            if not root:
                raise RuntimeError("no enabled Harness can run the lead: none takes the Bridge tools")
        source = read_object(topic_file(state, origin["topic_id"])) if origin.get("topic_id") else {}
        data = {key: value for key, value in root.items() if key not in LEAD_DROPS}
        chosen = lead_harness.binding(self.env.topics.apps, root, origin.get("lead"), butler_settings.load(state).lead)
        if (chosen["harness_id"], chosen["model"]) != (root.get("harness_id"), root.get("model")):
            data.pop("work_mode", None)  # The butler's model's own mode, if any: not the lead's model's.
        data.update(harness_id=chosen["harness_id"], model=chosen["model"], effort=chosen["effort"])
        name = source.get("workspace_name") or origin.get("workspace") or root.get("workspace_name") or ""
        workdir = source.get("workdir") or origin.get("workdir") or root.get("workdir") or ""
        if devices.is_device_value(workdir):
            # A workspace on a device is the project's topics' (project_lead.py device_workspace): the lead stays in the
            # butler's, where its tools are (core/harness.py DEVICE_OFF: bridge_tools).
            name, workdir = root.get("workspace_name") or "", root.get("workdir") or ""
        data.update(topic_id=lead, chat_id=chat, root_id="", thread_id="", binding_revision=1, project=field,
                    workspace_locked=True, harness_locked=True, model_settings_snapshot=True,
                    workspace_name=name, workdir=workdir)
        # The project's repositories (docs/configuration.md 主目录与仓库地图): those of the topic it grew from and those the
        # direct start named, read by the lead (it changes no file); a lead of the home says so as its topics do.
        repos = {}
        for repo in [*(source.get("repos") or ()), *(origin.get("repos") or ())]:
            if isinstance(repo, dict) and repo.get("name"):
                repos.setdefault(repo["name"], {**repo, "mode": "read"})
        if repos:
            data["repos"] = list(repos.values())
        if source.get("home_start") or origin.get("home_start") or (not source and root.get("home_start")):
            data.pop("started_at", None), data.pop("slug", None)  # The lead's own, not the butler's or the source topic's.
            made = record.get("created_at")
            mark_home(data, title=str(record.get("name") or ""),
                      now=made if isinstance(made, (int, float)) and not isinstance(made, bool) and made > 0 else None)
        with LOCK:
            write_json(path, {**read_object(path), **data}, ensure_ascii=False, indent=2)
        self.env.runtime.log(f"project lead bound project={pid[:8]} harness={chosen['harness_id']} from={chosen['source']}")
        return True

    def step_thread(self, record) -> bool:
        """The topic the project grew from is its first member: ``project`` {id, role thread (MEMBER)} in its topic.json, under its
        topic lock, and in the record's ``threads``."""
        state, topic = Path(self.env.state_dir), record["origin"]["topic_id"]
        with topic_lock(topic_ctx(state, topic)):
            set_topic_project(state, topic, record["id"], projects.MEMBER)
        revisits.drop_for_project(state, topic, self.env.runtime.log)
        projects.add_member(state, record["id"], topic)
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
                # another topic), and the 派发卡 it was belonged to the topic that is now a member.
                move_topic_message(state, record["lead_topic_id"], chat, message)
            route_message(state, chat, message, record["lead_topic_id"], holder=topic)
        return True

    def step_members(self, record) -> bool:
        """整理专项's 建专项 and a direct start with ``topics``: each topic it gathers is a member (``join_topic`` without its
        receipt: the start receipt names them). Its topic.json and the record's threads only: a running turn goes on and a
        question waiting for the owner stays his (app/triage.py routes a question when it is shown, not afterwards)."""
        state, pid = Path(self.env.state_dir), record["id"]
        for topic in (record.get("origin") or {}).get("topics") or ():
            found = projects.topic_project(state, topic)
            if found is not None and found.id != pid:
                continue  # Another project's since it was proposed: left there.
            with topic_lock(topic_ctx(state, topic)):
                set_topic_project(state, topic, pid, projects.MEMBER)
            revisits.drop_for_project(state, topic, self.env.runtime.log)
            logged = projects.add_member(state, pid, topic).get("joins") or ()
            if not any(isinstance(item, dict) and item.get("topic") == topic for item in logged):
                direct = (record.get("origin") or {}).get("kind") == "direct"
                projects.log_join(state, pid, topic, "start" if direct else "tidy", "主人建专项时说要收进来" if direct else "")
                tidy = getattr(self.env, "tidy", None)
                if direct and tidy is not None:
                    tidy.show_joined(topic, pid)  # 已归进「X」 · 移出 on its cards, as a join of 整理专项 has.
        return True

    def step_receipt(self, record) -> bool:
        """The start receipt in the lead's inbox: an upgrade says what the lead takes over, a direct start has the owner's
        request as he wrote it, 整理专项's the topics it gathered. Once, whatever happened to the step before. It carries the
        owner's message the project came of (``owner_message``), so the lead's first turn is his (WAVE12 C5, W2-C)."""
        state, pid, origin = Path(self.env.state_dir), record["id"], record.get("origin") or {}
        if not has_receipt(state, pid, "start"):
            text = (self.handover(state, record) if origin.get("kind") == "upgrade"
                    else self.gathered(state, record) if origin.get("kind") == "tidy"
                    else self.started(state, record))
            project_lead.add_receipt(state, pid, "start", text, thread=origin.get("topic_id") or "",
                                     message_id=owner_message(state, record))
        return True

    @staticmethod
    def started(state, record) -> str:
        """What the lead of a direct start is told: the owner's request as he wrote it, then the topics it took in, each with
        where it stands, and the ones it could not with why."""
        origin = record.get("origin") or {}
        text = origin.get("text") or record.get("goal") or record.get("name")
        threads = record.get("threads") or ()
        joined = [topic_line(state, topic) for topic in origin.get("topics") or () if topic in threads]
        missed = [f"{item.get('ref')}（{item.get('why')}）" for item in origin.get("refused") or ()]
        missed += [f"{topic_label(state, topic) or topic[:6]}（已经在别的专项里）" for topic in origin.get("topics") or ()
                   if topic not in threads]
        if joined:
            text += "\n已挂进这个专项的话题（主人要收进来的，你接手跟进）：" + "；".join(joined)
        if missed:
            text += "\n主人要收、但没挂上的：" + "；".join(missed)
        return text

    @staticmethod
    def gathered(state, record) -> str:
        """What the lead of a project 整理专项 made is told: the goal and the topics it takes over, each with where it stands."""
        parts = [f"主人同意把这几个已有的话题建成专项「{cut(record.get('name'), 30)}」，你接手跟进"]
        if record.get("goal"):
            parts.append("目标：" + cut(record["goal"], 160))
        parts.extend(topic_line(state, topic) for topic in record.get("threads") or ())
        parts.append(GATHERED_NOTE)
        return "；".join(parts)

    def gather(self, *, chat, name, goal, topics, key, why="", owner_named=False) -> str:
        """整理专项's 建专项: a project of the existing ``topics`` (GATHER_STEPS), named ``name``; its id, '' when MAX_ACTIVE
        projects are on and none could be folded away for it (app/project_loop.py make_room). ``key`` (the proposal's id)
        makes the id: the same click twice finds the same project."""
        state = Path(self.env.state_dir)
        topics = [topic for topic in dict.fromkeys(topics) if isinstance(topic, str) and projects.TOPIC_RE.fullmatch(topic)]
        if not topics:
            return ""
        pid = project_id("tidy", key)
        if not projects.read_record(state, pid):
            if not project_loop.make_room(state, self.env.runtime.log).ok:
                self.env.runtime.log(f"project gather skipped: {projects.MAX_ACTIVE} projects are on")
                return ""
            source = read_object(topic_file(state, topics[0]))
            made = projects.new_record(chat_id=chat, name=name or topic_title(state, topics[0]) or "专项", goal=goal,
                                       project_id=pid, origin={"kind": "tidy", "proposal": key, "topic_id": topics[0],
                                                               "topics": topics, "why": cut(why, 200),
                                                               "workspace": source.get("workspace_name") or "",
                                                               "workdir": source.get("workdir") or ""})
            made["steps"] = {"record": round(time.time(), 3)}
            if owner_named:
                made["name_by"] = "owner"  # 改个名再建: 整理专项 never renames it (core/project_proposals.py owner_named).
            self.seed_style(made)
            projects.create(state, made)
        self.advance(pid)
        return pid

    @staticmethod
    def handover(state, record) -> str:
        """What the lead is told it takes over: the topic, what the owner asked for, how it stands."""
        topic = record["origin"]["topic_id"]
        label, title = topic_label(state, topic), topic_title(state, topic)
        parts = ["接手一个已在进行的事：话题 " + " ".join(part for part in (label, f"「{cut(title, 30)}」" if title else "") if part)]
        if record.get("goal"):
            parts.append("主人要的：" + cut(record["goal"], 160))
        job = project_facts.newest_job(state, topic) or {}
        if job:
            parts.append("现在：" + cut(" ".join(part for part in (str(job.get("status") or ""), str(job.get("status_message") or "")) if part), 120))
        return "；".join(parts)

    # Direct ----------------------------------------------------------------------------------------------------------

    def start(self, request, args) -> dict:
        """topic_start with ``project`` true: the project's record, the lead's topic, the project's card (the owner's message
        above it) and the start receipt carrying his words; the lead then decides which member topics to open. Once a request
        (rule 6 of app/butler_topics.py: its answer is kept in the request's record)."""
        from covey.app import butler_handoff, butler_topics

        env, state = self.env, Path(self.env.state_dir)
        chat = env.chat()
        path = butler_opened.record_path(state, request)
        saved = read_object(path)
        if saved.get("answer"):
            return saved["answer"]
        pid = project_id("direct", butler_opened.request_token(request))
        record, room = projects.read_record(state, pid), project_loop.Room(True, "", "")
        if not record:
            settings = butler_settings.load(state)
            plan = butler_topics.plan_start(env, args, settings)
            if isinstance(plan, str):
                return reply(False, plan)
            lead = lead_wanted(env, args, plan)
            if isinstance(lead, str):
                return reply(False, lead)
            room = project_loop.make_room(state, env.runtime.log)
            if not room.ok:
                return reply(False, room.words + "。这次没开；也可以把这件事当普通话题开（topic_start 不带 project）。")
            joins, refused = wanted_topics(env, chat, args.get("topics"))
            title, prompt = arg_text(args.get("title"), 40), arg_text(args.get("prompt"))
            words = butler_handoff.asked(state, request.job_key).words
            made = projects.new_record(
                chat_id=chat, name=title, goal=prompt, project_id=pid,
                origin={"kind": "direct", "request": request.id, "job_key": request.job_key, "workspace": plan["workspace"],
                        "workdir": plan["workdir"], **({"repos": plan["repos"]} if plan.get("repos") else {}),
                        **({"home_start": True} if plan.get("home_start") else {}),
                        "why": arg_text(args.get("why"), 200), "text": words or prompt,
                        **({"topics": joins} if joins else {}), **({"refused": refused} if refused else {}),
                        **({"lead": lead} if lead else {})})
            made["steps"] = {"record": round(time.time(), 3)}
            ask = dispatch_cards.ask_of(env, state, request.job_key)
            asked = dispatch_cards.request_words(state, request.job_key)
            made["ask"] = {key: value for key, value in (("text", asked), ("at", ask.get("ask_at")), ("url", ask.get("ask_url")))
                           if value}
            self.seed_style(made)
            record, _ = projects.create(state, made)
        done = self.advance(pid)
        record = projects.read_record(state, pid) or record
        sent = bool(projects.card_message(record))
        note = {"request": request.id, "job_key": request.job_key, "chat_id": chat, "project_id": pid, "at": time.time()}
        text = (f"已开专项「{record.get('name')}」（{pid}）：负责人会开话题去做，专项卡在聊天区，进展看卡；不用等它做完。"
                if done else f"专项「{record.get('name')}」已建（{pid}），"
                             f"{'' if sent else '专项卡还没发出去，'}有一步没做完，Bridge 会自己补；不要再开一次。")
        text += project_loop.room_words(room)
        origin, threads = record.get("origin") or {}, record.get("threads") or ()
        joined = [topic for topic in origin.get("topics") or () if topic in threads]
        waiting = [topic for topic in origin.get("topics") or () if topic not in threads]
        refused = list(origin.get("refused") or ())
        if joined:
            text += "挂进专项的已有话题：" + "、".join(topic_label(state, topic) or topic[:6] for topic in joined) + "（不打断它们）。"
        if waiting:
            text += ("还没挂上：" if not done else "没挂上（已在别的专项里）：") + "、".join(
                topic_label(state, topic) or topic[:6] for topic in waiting) + "。"
        if refused:
            text += "没挂上：" + "；".join(f"{item['ref']}，{item['why']}" for item in refused) + "。把这些告诉主人。"
        return butler_opened.done(path, note, reply(True, text, project_id=pid, lead_topic_id=record.get("lead_topic_id"),
                                                  card=sent, finished=done, joined=joined,
                                                  refused=[item["ref"] for item in refused] + waiting))
