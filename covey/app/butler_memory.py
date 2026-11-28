"""The memory tools as the Bridge runs them (docs/butler.md 工具清单): memory_save, memory_revise and memory_search, the one
set that took the place of principle_* and knowledge_*, and the lead's project_remember in its new words.

An older session still calls the older names; core/butler_tool_specs.py RENAMED_TOOLS brings them here with their
arguments as they were, and each is said in the new words first. app/butler_knowledge.py ``memory_save(env, args) -> dict``
(and the two others) answers them once it is there; until then they are run as the principles (规矩, core/principles.py
through app/butler_tools.py) and the knowledge entries (资料, 决定; app/butler_knowledge.py) were. A module of its own:
app/butler_knowledge.py imports app/butler_tools.py, so the handlers that call into it cannot be there.
"""
from __future__ import annotations

import dataclasses
from types import SimpleNamespace

from covey.app import butler_knowledge, project_tools
from covey.app.butler_env import reply
from covey.app.butler_tools import (answer_of, principle_delete, principle_row, principle_save, principles_list,
                                    workspace_spaces)
from covey.core import principles
from covey.core.butler_tool_specs import MEMORY_KINDS, MEMORY_SCOPES

KNOWLEDGE_KINDS = ("decision", "practice", "pitfall", "fact", "question")  # core/knowledge.py's: an old knowledge_save call.
LEGACY_SCOPES = ("owner", "workspace")


def memory_handler(name):
    found = getattr(butler_knowledge, name, None)
    return found if callable(found) else None


def legacy_save(args) -> str:
    """The old tool a memory_save call's arguments are of: "principle", "knowledge", or "" for the new words."""
    if args.get("scope") not in LEGACY_SCOPES:
        return ""
    return "knowledge" if args.get("kind") in KNOWLEDGE_KINDS else "principle"


def new_save(args) -> dict:
    """An old principle_save or knowledge_save call's arguments in memory_save's words (a decision a note: memory_save
    takes none). It carries no quote: memory_save asks for the owner's words again."""
    kind = {"principle": "rule", "knowledge": "note"}[legacy_save(args)]
    workspace = str(args.get("workspace") or "").strip() if args.get("scope") == "workspace" else ""
    return {key: value for key, value in {"text": args.get("text"), "kind": kind, "scope": "global", "why": args.get("why"),
                                          "repos": [workspace] if workspace else None}.items() if value}


def memory_save(env, request):
    args = request.args if isinstance(request.args, dict) else {}
    made = memory_handler("memory_save")
    if made is not None:
        return answer_of(made(env, new_save(args) if legacy_save(args) else args, request))
    old = legacy_save(args)
    if old == "principle":
        return principle_save(env, request)
    if old == "knowledge":
        return butler_knowledge.knowledge_save(env, request)
    kind, scope = args.get("kind"), args.get("scope")
    if kind not in MEMORY_KINDS or scope not in MEMORY_SCOPES:
        raise ValueError("kind 要是 " + "、".join(MEMORY_KINDS) + " 之一，scope 要是 " + "、".join(MEMORY_SCOPES) + " 之一")
    if scope != "global":
        return reply(False, "这一版还记不到专项或话题：专项里的事交给它的负责人（project_send），或主人同意后记成通用的。")
    repos = [str(name).strip() for name in args.get("repos") or () if str(name).strip()]
    # One repository that is a saved workspace: the older store's workspace scope; any other, 通用.
    legacy = ({"scope": "workspace", "workspace": repos[0]} if len(repos) == 1 and repos[0] in workspace_spaces(env)[0]
              else {"scope": "owner"})
    if kind == "rule":
        return principle_save(env, with_args(request, {**legacy, "text": args.get("text")}))
    return butler_knowledge.knowledge_save(env, with_args(request, {
        **legacy, "kind": "decision" if kind == "decision" else "fact", "text": args.get("text"),
        **({"why": args["why"]} if args.get("why") else {})}))


def new_revise(args) -> dict:
    """An old principle_delete, knowledge_update or knowledge_drop call's arguments in memory_revise's words."""
    action = "supersede" if str(args.get("text") or "").strip() else "retire"
    return {key: value for key, value in {"key": str(args.get("id") or "").strip(), "action": action,
                                          "text": args.get("text"), "why": args.get("reason")}.items() if value}


def memory_revise(env, request):
    args = request.args if isinstance(request.args, dict) else {}
    old = "id" in args and "key" not in args
    made = memory_handler("memory_revise")
    if made is not None:
        return answer_of(made(env, new_revise(args) if old else args))
    if old:
        if str(args.get("id") or "").startswith("k"):
            handler = butler_knowledge.knowledge_update if "text" in args else butler_knowledge.knowledge_drop
            return handler(env, request)
        return principle_delete(env, request)
    key, action, text = str(args.get("key") or "").strip(), args.get("action"), str(args.get("text") or "").strip()
    if action not in ("update", "supersede", "retire"):
        raise ValueError("action 要是 update、supersede、retire 之一")
    if action != "retire" and not text:
        raise ValueError(f"{action} 要写新的 text")
    if key.startswith("k"):
        if action == "retire":
            return butler_knowledge.knowledge_drop(env, with_args(request, {"id": key, "reason": "不再成立"}))
        return butler_knowledge.knowledge_update(env, with_args(request, {"id": key, "text": text, "reason": "主人要改"}))
    if key.startswith("p"):
        item = next((row for row in principles.entries(env.state_dir) if row["id"] == key), None)
        if item is None:
            return reply(False, f"没有 {key[:20]} 这条：用 memory_search 找。")
        if action != "retire":
            saved = principle_save(env, with_args(request, {"scope": item["scope"], "workspace": item.get("workspace", ""),
                                                            "text": text}))
            if not saved.get("ok"):
                return saved
        gone = principle_delete(env, with_args(request, {"id": key}))
        return gone if action == "retire" else reply(True, f"已改：{saved['text']}（取代 {key}）", **saved.get("data", {}))
    return reply(False, f"没有 {key[:20]} 这条：用 memory_search 找到它的短码或 id。")


LEAD_NO_SEARCH = "这一版的记忆查不了专项范围：用状态包里的专项记忆。"


def memory_search(env, request):
    """memory_search for the butler, and for a project's lead (WAVE12 W2-C, core/butler_tool_specs.py LEAD_SHARED): a lead's
    call (app/butler_topics.py lead_of) reads only its own project's memory, whatever it names (app/butler_knowledge.py
    memory_search ``lead``)."""
    from covey.app.butler_topics import lead_of

    args = request.args if isinstance(request.args, dict) else {}
    made = memory_handler("memory_search")
    found = lead_of(env, request)
    if found is not None:
        if made is None:
            return reply(False, LEAD_NO_SEARCH)
        try:
            return answer_of(made(env, dict(args), lead=found[0]))
        except ValueError as refused:
            return reply(False, str(refused))
    if made is not None:
        return answer_of(made(env, dict(args)))  # As called: query, limit and whatever narrows it (project, topic, kinds…).
    if "query" not in args:  # An older session's principles_list.
        return principles_list(env, request)
    words = [word for word in str(args.get("query") or "").lower().split() if word]
    rows = [principle_row(item) for item in principles.entries(env.state_dir)
            if not words or any(word in item["text"].lower() for word in words)]
    found = butler_knowledge.knowledge_search(env, request)
    lines = [*(f"规矩 · {row}" for row in rows), *([found["text"]] if found.get("ok") and found["data"].get("count") else [])]
    return reply(True, "\n".join(lines) if lines else "没有找到相关的记忆。", count=len(rows) + int(found["data"].get("count") or 0))


def with_args(request, args):
    """``request`` with ``args`` in place of its own (a call run as an older tool)."""
    return dataclasses.replace(request, args=args) if dataclasses.is_dataclass(request) else SimpleNamespace(
        **{**vars(request), "args": args})


# The lead's project_remember in its new words (kind rule or note, and a title: docs/projects.md 专项记忆) while
# app/project_tools.py still takes its older kinds: rule as a constraint, note as a fact. Once it takes them, as it is.

def project_remember(env, request):
    args = request.args if isinstance(request.args, dict) else {}
    kind = args.get("kind")
    if kind in ("rule", "note") and kind not in getattr(project_tools, "MEMORY_KINDS", ()):
        title = str(args.get("title") or "").strip()
        text = str(args.get("text") or "").strip()
        request = with_args(request, {**args, "kind": "constraint" if kind == "rule" else "fact",
                                      "text": f"{title}：{text}" if title and not text.startswith(title) else text})
    return project_tools.TOOLS["project_remember"](env, request)


TOOLS = {"memory_save": memory_save, "memory_revise": memory_revise, "memory_search": memory_search,
         "project_remember": project_remember}
