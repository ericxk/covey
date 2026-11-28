"""What Harness, model and budget a project's lead runs with (docs/projects.md 负责人用哪个 Harness), COVEY_PROJECTS=1.

A new lead (app/projects.py step_lead) takes, in this order, the first of these that can run it: what the butler's call
named for it (topic_start with ``project`` true and a ``harness`` or ``model``, kept in the record's origin as ``lead``),
the settings' (core/butler_settings.py LeadModel, the 设置 card's 专项负责人), else the butler's own (its topic.json, else
what its first message would write). A Harness can run a lead when it is enabled here, takes the Bridge's tools
(core/harness.py bridge_tools: the lead's tools, and the read-only hold app/tool_limits.py puts on its turn).

A lead that is there keeps its own until the owner changes it: 换配置 on the 专项卡 (app/harness_switch.py) or on the web page
(app/web_switch.py), both of which offer only the menu's models ``can_lead`` allows, and a workspace the lead keeps.
"""
from __future__ import annotations

from pathlib import Path

from covey.core import projects
from covey.core.harness import backend_capabilities
from covey.core.text import cut
from covey.core.topics import load_topic

# Said where a model cannot run a lead: on the 换配置 card, by the web page's switch, to the butler's topic_start.
CANNOT_LEAD = "负责人只能用能接 Bridge 工具的 Harness（和管家能选的一样），它要用这些工具派话题，并在上面守住只读"


def can_lead(entry) -> bool:
    """Whether the menu entry ``entry`` (app/launch_models.py available_models) is a model a lead can run on: its Harness
    takes the Bridge's tools."""
    return bool(entry) and bool(entry.get("bridge_tools"))


def lead_choices(choices) -> dict:
    """``choices`` (the model menu) left with the models a lead can run on (``can_lead``), in their order."""
    return {key: entry for key, entry in choices.items() if can_lead(entry)}


def runs_lead(app) -> bool:
    """Whether the Harness app ``app`` (None: not enabled here) can run a lead: it takes the Bridge's tools on this machine."""
    harness = getattr(app, "harness", None)
    if app is None or harness is None:
        return False
    return bool(backend_capabilities(app.backend, harness.context).bridge_tools)


def refusal(apps, name) -> str:
    """Why the Harness ``name`` cannot run a lead here, '' when it can (``runs_lead``)."""
    app = apps.get(name)
    if app is None:
        return f"本机器人没有启用 {name}"
    if not runs_lead(app):
        return CANNOT_LEAD
    return ""


def wanted_of(value) -> dict:
    """{harness, model, effort} of a choice saved for a lead (the record's origin ``lead``, or the settings' LeadModel as a
    mapping), the texts stripped; {} when it names no Harness."""
    if hasattr(value, "harness"):
        value = {"harness": value.harness, "model": value.model, "effort": value.effort}
    if not isinstance(value, dict):
        return {}
    found = {key: value.get(key).strip() if isinstance(value.get(key), str) else "" for key in ("harness", "model", "effort")}
    return found if found["harness"] else {}


def binding(apps, root, call=None, settings=None) -> dict:
    """{harness_id, model, effort, source} a new lead runs with: ``call`` (what the butler's call named for it), else
    ``settings`` (the settings' LeadModel), each as ``wanted_of`` reads it and taken only while its Harness can run a lead
    (``runs_lead``); else ``root``'s own, the butler's. ``source``
    says which: "call", "settings" or "butler"."""
    for source, choice in (("call", wanted_of(call)), ("settings", wanted_of(settings))):
        if choice and runs_lead(apps.get(choice["harness"])):
            return {"harness_id": choice["harness"], "model": choice["model"], "effort": choice["effort"], "source": source}
    return {"harness_id": str(root.get("harness_id") or ""), "model": str(root.get("model") or ""),
            "effort": str(root.get("effort") or ""), "source": "butler"}


def target(topics, state_dir, topic_id, chat, project=None):
    """(app, context, topic.json, project record) of a project's lead ``topic_id`` in ``chat``, for a 换配置 of it (the 专项卡's,
    app/harness_switch.py, or the web page's, app/web_switch.py): the context that of the 专项卡's message, which routes to the
    lead (a reply to it is the lead's, quoting it at the chat's top level). ``project``: the project's id the click named
    (None: the one ``topic_id`` leads). None when it is not a lead of this chat (or projects are off), that project's card
    was never sent, or its topic.json or its Harness's app is missing."""
    state = Path(state_dir)
    record = projects.lead_record(state, str(topic_id or ""), chat or None)
    if not record or (project is not None and record.get("id") != project) or record.get("chat_id") != chat:
        return None
    message = projects.card_message(record)
    if not message:
        return None
    try:
        topic = load_topic(state, topic_id)
    except (OSError, ValueError):
        return None
    app = topics.apps.get(topic.get("harness_id"))
    if app is None:
        return None
    context = app.submission.event_context(app.harness.context, {"chat_id": chat, "message_id": message})
    return (app, context, topic, record) if context.topic_id == topic_id else None


def named(record) -> dict:
    """{project_id, name} of the project ``record`` as a 换配置 record keeps it (app/harness_switch.py ``lead``): its words
    name the project."""
    return {"project_id": str(record.get("id") or ""), "name": cut(str(record.get("name") or ""), 30)}
