"""协作的预检 (docs/architecture.md 多参与者协作 · 能力与预检, COVEY_COLLAB): whether a collaboration may start, who its
participants are, and each helper as it is now. It reads records, topic.json and the registry; it writes nothing and
sends nothing (app/collab_flow.py writes what it decided, outside any lock it took here).

- ``participant``: a topic id as a participant (core/collab.py Participant): the butler (slot 0), a project's lead, or a
  topic (an ordinary one or a project's); a track slot of the butler, a 代答 topic and a 咨询通道 are none.
- ``resolve``: a name a checkpoint gives (T5, 管家, 负责人) as a topic id of the chat.
- ``admission``: the integrator's side (rule 11's ``agent_chat``: the owner's private chat only, no group, no 代答 chat;
  the switch; its Harness declares ``collab``; not on a device).
- ``helper_refusal``: what never changes while the collaboration lives (not the integrator itself, same chat, not 代答,
  not retired, not on a device, its Harness declares ``collab`` and ``read_only``, 敏感词脱敏 on both sides or on
  neither, another project only when the owner named it himself).
- ``receiver_now``: the helper as it is now: ``BUSY`` (a task queued or running: the ask waits, a supplement never
  takes it), ``IDLE`` (it may be asked), or why it is not taken (it waits for the owner's answer, it never started, the
  owner stopped it).
"""
from __future__ import annotations

import re
from pathlib import Path

from covey.core import butler, collab, projects, turn_source
from covey.core.harness import backend_capabilities
from covey.core.jobs import never_started, topic_job_records
from covey.core.look import owner_stopped
from covey.core.state import read_object
from covey.core.statuses import ENDED, RUNNING
from covey.core.topics import label_title, retired_topic, topic_file, topic_label

IDLE, BUSY = "idle", "busy"

OFF = "协作没有打开（COVEY_COLLAB）"
NOT_PRIVATE = "协作只在主人的私聊里发起"
NOT_A_TOPIC = "这里不能发起协作（管家同时做的几件事、代答话题、咨询通道都不行）"
INTEGRATOR_DEVICE = "这个话题在设备上，首版协作不支持"
INTEGRATOR_HARNESS = "这个话题的 {title} 还不支持协作（汇总方要能跑协作续接轮）"
TOO_MANY = collab.TOO_MANY
NOT_FOUND = "找不到 {name}"
SELF = "不能请自己协助"
OTHER_CHAT = "{label} 不在这个私聊里"
PROXY = "{label} 是代答话题，不参与协作"
RETIRED = "{label} 已经下线"
DEVICE = "对方话题在设备上，首版不支持只读咨询"
HARNESS = "{label} 用的 {title} 还不能只读咨询（首版只有通过真实拒写验收的 Harness）"
OTHER_PROJECT = "{label} 在别的专项里：跨专项只在主人这次亲自点名时才问"
NOT_PARTICIPANT = "{label} 不能当协助方"
WAITING_OWNER = "对方在等主人回答问题，没有接手（不替主人回答、不动那道题）"
NEVER_RAN = "对方还没开始"
STOPPED = "对方话题的任务被主人停了，不唤醒它"
HELPER_ASKS = "协助方不能再去请别人"


def _harness_of(state, topic) -> str:
    return str(read_object(topic_file(Path(state), topic)).get("harness_id") or "")


def participant(state, chat, topic) -> collab.Participant | None:
    """``topic`` of ``chat`` as a participant, None when it is none (no topic.json, another chat's, a track of the
    butler, a 代答 topic, a 咨询通道)."""
    if not isinstance(topic, str) or not topic or "/" in topic or collab.is_channel(topic):
        return None
    if butler.is_butler_root(topic, chat):
        return collab.Participant(topic, collab.BUTLER)
    if butler.is_lead(topic, chat):
        found = projects.topic_project(state, topic)
        return collab.Participant(topic, collab.LEAD, found.id if found else "")
    if butler.is_butler_topic(topic, chat):
        return None  # A track of the butler: the butler is slot 0's.
    data = read_object(topic_file(Path(state), topic))
    if not data or data.get("chat_id") != chat or data.get("proxy"):
        return None
    found = projects.topic_project(state, topic)
    return collab.Participant(topic, collab.TOPIC, found.id if found is not None and found.role == projects.MEMBER else "")


def label(state, chat, who) -> str:
    """How the owner reads ``who`` (a Participant): T5, 管家, 负责人（专项名）."""
    if who.role == collab.BUTLER:
        return "管家"
    if who.role == collab.LEAD:
        name = (projects.read_record(state, who.project) or {}).get("name") if who.project else ""
        return f"负责人（{name}）" if name else "负责人"
    return topic_label(Path(state), who.key) or label_title(Path(state), who.key) or who.key[:8]


NUMBER = re.compile(r"[Tt](\d{1,7})")


def resolve(state, chat, name, here="") -> str:
    """The topic id ``name`` names in ``chat`` (``T5``, ``管家``/``butler``, ``负责人``/``lead``: the lead of the project of
    ``here``), '' when none does."""
    text = "".join(str(name or "").split())
    if text in ("管家", "butler"):
        return butler.butler_topic_id(chat)
    if text in ("负责人", "lead"):
        found = projects.topic_project(state, here) if here else None
        return projects.lead_topic_id(chat, found.id) if found is not None else ""
    number = NUMBER.fullmatch(text)
    if not number:
        return ""
    try:
        paths = list((Path(state) / "topics").glob("*/topic.json"))
    except OSError:
        return ""
    for path in paths:
        if collab.is_channel(path.parent.name):
            continue
        data = read_object(path)
        if data.get("topic_number") == int(number.group(1)) and data.get("chat_id") == chat:
            return str(data.get("topic_id") or path.parent.name)
    return ""


def named(words, state, chat, who) -> bool:
    """Whether the owner named ``who`` himself in ``words`` (his own message): its T number standing alone, or 管家 /
    负责人 for those."""
    words = str(words or "")
    if who.role == collab.BUTLER:
        return "管家" in words
    if who.role == collab.LEAD:
        return "负责人" in words
    found = topic_label(Path(state), who.key)
    return bool(found) and re.search(rf"(?<![A-Za-z0-9]){re.escape(found)}(?![0-9])", words, flags=re.IGNORECASE) is not None


def capabilities(topics, state, topic):
    """(app, capabilities) of ``topic``'s Harness on this machine (a topic on a device is refused before: ``on_device``);
    (None, None) when its Harness has no app here."""
    app = topics.apps.get(_harness_of(state, topic))
    if app is None:
        return None, None
    return app, backend_capabilities(app.harness.backend, app.harness.context)


def on_device(state, topic) -> bool:
    from covey.core import devices

    value = str(read_object(topic_file(Path(state), topic)).get("workdir") or "")
    return devices.is_device_value(value)


def admission(access, topics, state, chat, integrator) -> str:
    """Why ``integrator`` (a Participant, None for none) may not start a collaboration in ``chat``, '' when it may."""
    if not turn_source.enabled():
        return OFF
    if not access.agent_chat(chat, group=False):
        return NOT_PRIVATE
    if integrator is None:
        return NOT_A_TOPIC
    if on_device(state, integrator.key):
        return INTEGRATOR_DEVICE
    app, caps = capabilities(topics, state, integrator.key)
    if app is None or not caps.collab:
        title = app.harness.spec.title if app is not None else (_harness_of(state, integrator.key) or "Harness")
        return INTEGRATOR_HARNESS.format(title=title)
    return ""


def helper_refusal(topics, state, chat, integrator, helper_key, *, owner_named) -> str:
    """Why the topic ``helper_key`` may not be asked in this collaboration of ``integrator`` (a Participant) at all, ''
    when it may (its state now is ``receiver_now``'s)."""
    if helper_key == integrator.key:
        return SELF
    who = participant(state, chat, helper_key)
    data = read_object(topic_file(Path(state), helper_key))
    shown = (label(state, chat, who) if who is not None else topic_label(Path(state), helper_key)) or helper_key[:8]
    if not data:
        return NOT_FOUND.format(name=shown)
    if data.get("chat_id") != chat and not butler.is_butler_topic(helper_key, chat):
        return OTHER_CHAT.format(label=shown)
    if data.get("proxy"):
        return PROXY.format(label=shown)
    if who is None:
        return NOT_PARTICIPANT.format(label=shown)
    if retired_topic(Path(state), helper_key):
        return RETIRED.format(label=shown)
    if on_device(state, helper_key):
        return DEVICE
    app, caps = capabilities(topics, state, helper_key)
    if app is None or not (caps.collab and caps.read_only):
        return HARNESS.format(label=shown, title=app.harness.spec.title if app is not None else
                              (_harness_of(state, helper_key) or "Harness"))
    if (who.project or integrator.project) and who.project != integrator.project and not owner_named:
        return OTHER_PROJECT.format(label=shown)
    return ""


def ran(records) -> list:
    """The ended records of tasks a backend ran (not a launch card's, not one cancelled while queued), oldest first."""
    return sorted((record for record in records if record.get("status") in ENDED and not never_started(record)),
                  key=lambda record: int(record.get("job_id") or 0))


def receiver_now(runtime, state, chat, topic) -> str:
    """The helper ``topic`` now: BUSY (a task of it is queued or running, here or in a task runner), IDLE, or why it is
    not taken (it waits for the owner's answer, it never started, the owner stopped its last task). A task of 协作's
    own (a source with no owner's words) neither waits for him nor was stopped by him: it does not count."""
    records = topic_job_records(Path(state), topic, chat_id=chat)
    if any(record.get("status") in RUNNING for record in records):
        return BUSY
    if any(record.get("status") == "waiting_user" for record in records):
        return WAITING_OWNER
    if runtime is not None and runtime.jobs.open_in_topic(topic, chat):
        return BUSY  # Registered here before its record says so.
    done = [record for record in ran(records) if turn_source.behavior(record).owner_words]
    if not done:
        return NEVER_RAN
    latest = done[-1]
    if latest.get("status") == "cancelled" and owner_stopped(latest):
        return STOPPED
    return IDLE


def last_ended(state, chat, topic) -> dict:
    """The helper's latest ended task a backend ran (its record), {} when none: what an ask's evidence is as of."""
    found = ran(topic_job_records(Path(state), topic, chat_id=chat))
    return found[-1] if found else {}
