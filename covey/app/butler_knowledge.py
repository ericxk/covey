"""The butler's knowledge-entry tools (core/knowledge.py, docs/butler.md 知识库) and history_search, the one
that asks the history command (COVEY_HISTORY_CMD). knowledge_save, knowledge_update, knowledge_drop and
knowledge_search work only with COVEY_KNOWLEDGE=1 and are listed only then (core/butler_tool_specs.py listed);
each takes the entry file's own lock and sends nothing to Feishu (rules 1 and 2).

memory_save, memory_revise and memory_search (docs/butler.md 记忆) are the memory's own tools, the one set that replaces
principle_* and knowledge_*: module-level functions ``(env, args) -> {ok, text, data}`` that app/butler_memory.py calls once
it has put an older call's arguments in the new words (they are not in ``TOOLS``). A save (T908 M3a) is a rule or a note
(no decision: the butler's has no question a turn could carry it by) and carries the owner's words of this turn it is
of (``quote``): app/memory_proof.py proves them in the butler's turn, and one it cannot is refused, so the butler writes
again with his words; it goes through core/memory_gate.py write (the one way in, ``strict``: a refusal comes back with
the rewrite offered) as the owner's own, by the butler, in the scope it chose by what it is about: 全局 only when his words
say so (core/memory_gate.py EVERYWHERE: 所有、以后都、处处), else a rule waits in 待审核 for his accept (a note, carried
only when related, goes 全局 as asked).

history_search runs ``<cmd> search [--workspace W] [--since Nd] -- <query>`` as a subprocess of its own in its own
process group, waits HISTORY_TIMEOUT seconds at most (the command has its own 60 s limit; this one is the Bridge's)
and stops the whole group when it is over, reads at most HISTORY_BYTES of its output (the command cuts at the same
size), and answers a command that is missing, not executable, slow or failing in one sentence. It runs on the
main-tools loop, so a slow search holds the butler's other calls for as long: they come one after another anyway.
"""
from __future__ import annotations

import re
import subprocess
import threading
from pathlib import Path

from covey.app import memory_proof
from covey.app.butler_env import reply
from covey.app.butler_tools import workspace_spaces
from covey.core import knowledge, memory_gate, memstore, projects
from covey.core.jobs import job_path, read_job_record
from covey.core.process import stop_group
from covey.core.state import read_object

HISTORY_TIMEOUT = 70  # Seconds.
HISTORY_BYTES = 20 * 1024
OFF = "知识条目没有开（这个部署设了 COVEY_KNOWLEDGE=0），这次调用没有执行。"
TOPIC_REF = re.compile(r"[Tt]?(\d+)")
SINCE = re.compile(r"\d{1,4}d")


def _on():
    return None if knowledge.enabled() else reply(False, OFF)


def _workspace(env, name, *, required=False):
    """The registered workspace ``name`` names ('' for none); ValueError with the names to choose from otherwise."""
    name = knowledge.clean_text(name)
    if not name:
        if required:
            raise ValueError("工作区的条目要写是哪个工作区（workspaces_list 里的名字）")
        return ""
    spaces, _ = workspace_spaces(env)
    if name not in spaces:
        raise ValueError(f"没有工作区“{name[:40]}”；可选：" + ("、".join(spaces) or "（没有）"))
    return name


def _source(env, request, topic=""):
    record = read_job_record(job_path(env.state_dir, request.job_key)) if request.job_key else None
    source = {"job_key": request.job_key, **({"job_id": record["job_id"]} if (record or {}).get("job_id") else {})}
    topic = knowledge.clean_text(topic)
    if topic:
        found = TOPIC_REF.fullmatch(topic)
        if not found:
            raise ValueError("source_topic 要写 T 号，如 T483")
        source["topic"] = "T" + found.group(1)
    return source


def knowledge_save(env, request):
    if (off := _on()) is not None:
        return off
    args = request.args
    scope = args.get("scope")
    workspace = _workspace(env, args.get("workspace"), required=True) if scope == knowledge.WORKSPACE else ""
    entry, new = knowledge.save(env.state_dir, scope, args.get("kind"), args.get("text"), workspace=workspace,
                                why=args.get("why"), source=_source(env, request, args.get("source_topic")))
    if not new:
        return reply(True, f"{knowledge.where_words(entry)}里已有这条（{entry['id']}）：{entry['text']}，没有重复记。",
                     id=entry["id"], new=False)
    return reply(True, f"已记下（{knowledge.row(entry)}）", id=entry["id"], new=True)


def knowledge_update(env, request):
    if (off := _on()) is not None:
        return off
    args = request.args
    entry = knowledge.update(env.state_dir, args.get("id"), args.get("text"), kind=args.get("kind") or None,
                             why=args.get("why") if "why" in args else None, reason=args.get("reason"),
                             source=_source(env, request))
    return reply(True, f"已改写：{knowledge.row(entry)}（取代 {entry['replaces']}）", id=entry["id"],
                 replaces=entry["replaces"])


def knowledge_drop(env, request):
    if (off := _on()) is not None:
        return off
    entry = knowledge.drop(env.state_dir, request.args.get("id"), request.args.get("reason"), source=_source(env, request))
    return reply(True, f"已作废：{knowledge.row(entry)}", id=entry["id"])


def knowledge_search(env, request):
    if (off := _on()) is not None:
        return off
    args = request.args
    workspace = _workspace(env, args.get("workspace"))
    history = args.get("history") is True
    rows = knowledge.search(env.state_dir, args.get("query"), workspace=workspace or None, history=history)
    if not rows:
        return reply(True, "没有找到匹配的知识条目" + ("" if history else "（加 history: true 也搜已取代、已作废的）") + "。", count=0)
    return reply(True, "\n".join(knowledge.row(entry, state=history) for entry in rows), count=len(rows))


def history_search(env, request):
    command = knowledge.history_cmd()
    if not command:
        return reply(False, "历史搜索没有配置（这个部署没设 COVEY_HISTORY_CMD），这次调用没有执行。")
    args = request.args
    query = knowledge.clean_text(args.get("query"))
    if not query:
        raise ValueError("要给搜索词（query）")
    workspace = knowledge.clean_text(args.get("workspace"))
    since = knowledge.clean_text(args.get("since"))
    if since and not SINCE.fullmatch(since):
        raise ValueError("since 写天数，如 30d")
    argv = [command, "search", *(["--workspace", workspace] if workspace else []), *(["--since", since] if since else []),
            "--", query]
    return run_history(argv, command)


def run_history(argv, command):
    """The answer to running ``argv``: its output, or one sentence on why there is none."""
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                start_new_session=True)
    except FileNotFoundError:
        return reply(False, f"历史搜索的命令不存在：{command}。检查 COVEY_HISTORY_CMD。")
    except PermissionError:
        return reply(False, f"历史搜索的命令不能执行：{command}。检查文件权限。")
    except OSError as exc:
        return reply(False, f"历史搜索的命令没能启动（{type(exc).__name__}）：{command}。")
    chunks = []
    reader = threading.Thread(target=lambda: chunks.append(proc.stdout.read(HISTORY_BYTES + 1)), daemon=True,
                              name="history-search-read")
    reader.start()
    reader.join(HISTORY_TIMEOUT)
    timed_out = reader.is_alive()
    raw = chunks[0] if chunks else b""
    cut = len(raw) > HISTORY_BYTES
    if timed_out or cut:
        stop_group(proc, 2)  # The whole group: the command's own children too.
    else:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            stop_group(proc, 2)
    reader.join(2)
    raw = chunks[0] if chunks else raw
    proc.stdout.close()
    if timed_out:
        return reply(False, f"历史搜索超过 {HISTORY_TIMEOUT} 秒没有结果，已经停掉：换个更具体的说法，或加 workspace、since 缩小范围再试。")
    text = raw[:HISTORY_BYTES].decode("utf-8", "replace").strip()
    if proc.returncode != 0 and not cut:
        return reply(False, f"历史搜索的命令出错了（退出码 {proc.returncode}）：" + (text[-500:] or "没有输出") + "。")
    if cut:
        text += f"\n\n（输出超过 {HISTORY_BYTES // 1024} KB，已截断；缩小范围再查，或用 workspace、since。）"
    return reply(True, text or "没有找到相关的历史轮次。", truncated=cut)


def _project_of(state_dir, name):
    """The id of the project ``name`` (its id or its name) names; '' when there is none."""
    name = knowledge.clean_text(name)
    if not name:
        return ""
    if projects.valid_id(name) and projects.read_record(state_dir, name):
        return name
    return next((row["id"] for row in projects.records(state_dir) if str(row.get("name") or "") == name), "")


def _topic_of(state_dir, name):
    """The id of the topic ``name`` (its id or its T number) names; '' when there is none."""
    name = knowledge.clean_text(name)
    base = Path(state_dir) / "topics"
    if re.fullmatch(r"[A-Za-z0-9_.-]+", name) and (base / name).is_dir():
        return name
    if name[:1] in ("T", "t") and TOPIC_REF.fullmatch(name):
        for path in sorted(base.glob("*/topic.json")):
            if read_object(path).get("topic_number") == int(name[1:]):
                return path.parent.name
    return ""


def _owner_of(env, scope, args):
    """(owner key, why not): the project or topic id ``args`` name for ``scope``."""
    if scope == "project":
        name = args.get("project")
        if not knowledge.clean_text(name):
            return "", "管专项的条目要写是哪个专项：加 project（专项 id 或名字）。"
        found = _project_of(env.state_dir, name)
        return (found, "") if found else ("", f"没有叫 {name} 的专项。")
    if scope == "topic":
        name = args.get("topic")
        if not knowledge.clean_text(name):
            return "", "管话题的条目要写是哪个话题：加 topic（话题 id 或 T 号）。"
        found = _topic_of(env.state_dir, name)
        return (found, "") if found else ("", f"没有 {name} 这个话题。")
    return "", ""


def _where(entry) -> str:
    return {"global": "管全局", "project": "管这个专项", "topic": "管这个话题"}.get(entry.scope, entry.scope)


def _refused(verdict) -> dict:
    text = verdict.reason or "这条写法不合适"
    return reply(False, text + (f"。可以改成：{verdict.text}" if verdict.text else "") + "。没有记。", refused=True)


SAVE_KINDS = ("rule", "note")
UNPROVEN = ("核对不到主人这一轮的原话：quote 要照抄主人这一轮说这件事的一段（一字不改，不是你的转述），照他的原话再记一次。"
            "没有记。")
NO_DECISION = "kind 只能是 rule（规矩）或 note（资料）：主人答过的题 Bridge 已记成决定，不用记。没有记。"


def memory_save(env, args, request=None):
    """memory_save: one rule or note in the memory, of the owner's words of this turn (``quote``, proven in the turn of
    ``request``), through core/memory_gate.py write (the module's docstring)."""
    kind, scope = str(args.get("kind") or "note"), str(args.get("scope") or "global")
    text, title = knowledge.clean_text(args.get("text")), knowledge.clean_text(args.get("title"))
    quote = knowledge.clean_text(args.get("quote"))
    if kind not in SAVE_KINDS:
        return reply(False, NO_DECISION, refused=True)
    if not text:
        return reply(False, "要写的内容（text）是空的。")
    owner, why_not = _owner_of(env, scope, args)
    if why_not:
        return reply(False, why_not)
    access = getattr(getattr(env, "runtime", None), "access", None)
    proof = memory_proof.prove(access, env.state_dir, memory_gate.Proof(quote=quote, via="butler"),
                               job_key=str(getattr(request, "job_key", "") or "")) if quote and access is not None else None
    if proof is None:
        return reply(False, UNPROVEN, refused=True)
    asked = kind == "rule" and scope == "global" and not memory_gate.everywhere(quote)  # A rule's 全局 he did not ask for: 待审核.
    try:
        written = memory_gate.write(env.state_dir, kind=kind, scope=scope, owner_key=owner, text=text, title=title,
                                    why=knowledge.clean_text(args.get("why")), repos=tuple(args.get("repos") or ()),
                                    proof=proof, by="butler", source="memory_save", place="butler", strict=True,
                                    pending=asked)
    except ValueError as exc:
        return reply(False, str(exc))
    if written.reason == memory_gate.PENDING:
        return reply(True, "主人原话没说“所有”“以后都”“处处”，这条放进了待审核，主人采纳后才管全局；只关某个专项或话题的，"
                           "改记到那个专项或话题（scope project 或 topic）。", review=written.review, flags=list(written.flags))
    if written.entry is None:
        return _refused(memory_gate.Verdict(False, written.rewrite, written.flags, written.reason))
    entry = written.entry
    said = {memstore.CONFIRMED: "（原来是负责人记的同一条，现在算主人定的）", memstore.SAME: "（已经记过这一条）"}.get(written.done, "")
    return reply(True, f"记下了：{entry.code} {entry.title} · {_where(entry)}{said}", code=entry.code, ref=entry.ref,
                 flags=list(written.flags))


OWNER_RULE = ("这是主人定的规矩：改、替代或收起它要带主人这一轮说这件事的原话（quote，一字不改，不是你的转述）；"
              "主人这一轮没说要改它就先问主人。没有改。")


def memory_revise(env, args, request=None):
    """memory_revise: update, supersede or retire the entry ``key`` names. A rule of the owner's own (你定的) is changed only
    with his words of this turn (``quote``, proven in the turn of ``request`` as memory_save's are), the lead's protection
    (T908 M3b); anybody else's, or a note or a decision, without."""
    key, action = knowledge.clean_text(args.get("key")), str(args.get("action") or "")
    entry = memstore.find(env.state_dir, key) if key else None
    if entry is None:
        return reply(False, f"没有 {key}：先用 memory_search 找短码。")
    if action not in ("update", "supersede", "retire"):
        return reply(False, "action 只能是 update、supersede、retire。")
    if entry.kind == "rule" and entry.said == "owner":
        quote = knowledge.clean_text(args.get("quote"))
        access = getattr(getattr(env, "runtime", None), "access", None)
        proof = memory_proof.prove(access, env.state_dir, memory_gate.Proof(quote=quote, via="butler"),
                                   job_key=str(getattr(request, "job_key", "") or "")) if quote and access is not None else None
        if proof is None:
            return reply(False, OWNER_RULE, refused=True)
    text, title = knowledge.clean_text(args.get("text")), knowledge.clean_text(args.get("title"))
    if action != "retire":
        if not text and not (action == "update" and (title or args.get("why") is not None)):
            return reply(False, "要改成什么（text）没写。")
        if text:
            verdict = memory_gate.check(text, kind=entry.kind, title=title)
            if not verdict.ok:
                return _refused(verdict)
    state = env.state_dir
    try:
        if action == "update":
            fresh = memstore.edit(state, entry.ref, text=text or None, title=title or None,
                                  why=knowledge.clean_text(args.get("why")) if args.get("why") is not None else None,
                                  who="butler")
            return reply(True, f"改好了：{fresh.code} {fresh.title} · {_where(fresh)}", code=fresh.code, ref=fresh.ref)
        if action == "supersede":
            if entry.kind == "decision":
                new = memstore.add_decision(state, scope=entry.scope, owner_key=entry.owner_key, text=text,
                                            title=title, said="owner", by="butler")
            else:
                new = memstore.add(state, kind=entry.kind, scope=entry.scope, owner_key=entry.owner_key, text=text,
                                   title=title, why=knowledge.clean_text(args.get("why")), said="owner", by="butler",
                                   repos=entry.repos, who="butler")
            memstore.supersede(state, entry.ref, new.ref, who="butler")
            return reply(True, f"换成新的了：{new.code} {new.title}（{entry.code} 已收起）", code=new.code, ref=new.ref,
                         old=entry.ref)
        done = (memstore.settle if entry.kind == "decision" else memstore.archive)(state, entry.ref, who="butler")
    except ValueError as exc:
        return reply(False, str(exc))
    return reply(True, f"{done.code} {done.title} 已" + ("办完" if entry.kind == "decision" else "收起"), code=done.code,
                 ref=done.ref)


KIND_WORDS = {"rule": "规矩", "decision": "决定", "note": "资料"}
SEARCH_STATUS = ("active", "done", "archived")
STATE_WORDS = {"done": "（已办）", "archived": "（已收起）"}
SEARCH_LIMIT, SEARCH_MAX, SEARCH_TEXT = 8, 20, 120


LEAD_ELSEWHERE = "负责人只能查本专项的记忆（本专项和它的话题的）：{what}不是这个专项的，这次没有查。"


def lead_scope(env, args, lead) -> tuple:
    """(project, topic) a project's lead's memory_search reads (WAVE12 W2-C): its own project ``lead``, and a topic only of
    that project (its lead's or a member's); ValueError for any other, named: no other project's line, no 全局 one, no
    ordinary topic's (core/memory_search.py reads with ``owner`` False)."""
    if args.get("project") and _project_of(env.state_dir, args.get("project")) != lead:
        raise ValueError(LEAD_ELSEWHERE.format(what=f"专项 {str(args.get('project'))[:40]} "))
    topic = _topic_of(env.state_dir, args.get("topic")) if args.get("topic") else ""
    if args.get("topic"):
        found = projects.topic_project(Path(env.state_dir), topic) if topic else None
        if found is None or found.id != lead:
            raise ValueError(LEAD_ELSEWHERE.format(what=f"话题 {str(args.get('topic'))[:40]} "))
    return lead, topic


def memory_search(env, args, *, lead=""):
    """memory_search: the entries that answer ``query`` (the most recent ones with none). For a project's lead (``lead``,
    its project's id: app/butler_memory.py memory_search) only its own project's and its topics' (``lead_scope``), read as
    a turn not the owner's reads them."""
    from covey.core import memory_search as ranking
    query = knowledge.clean_text(args.get("query"))
    try:
        limit = max(1, min(int(args.get("limit") or SEARCH_LIMIT), SEARCH_MAX))
    except (TypeError, ValueError):
        limit = SEARCH_LIMIT
    if lead:
        project, topic = lead_scope(env, args, lead)
    else:
        project = _project_of(env.state_dir, args.get("project")) if args.get("project") else ""
        topic = _topic_of(env.state_dir, args.get("topic")) if args.get("topic") else ""
    kinds = tuple(k for k in (args.get("kinds") or ()) if k in KIND_WORDS)
    if query:
        context = {"owner": not lead, "project": project, "topic": topic, "statuses": SEARCH_STATUS,
                   **({"kinds": kinds} if kinds else {})}
        rows = ranking.eval_search(env.state_dir, query, context, limit)
        states = {}
        for row in rows:  # The search says each hit's status; looked up only when it does not.
            found = None if row.get("status") else memstore.find(env.state_dir, row["ref"])
            states[row["ref"]] = row.get("status") or (found.status if found else "active")
    else:
        scope, owner = ("project", project) if project else ("topic", topic) if topic else ("", "")
        if lead:  # The topic it names, else its project; never a line kept back from what is sent out (redacted).
            scope, owner = ("topic", topic) if topic else ("project", lead)
        found = memstore.entries(env.state_dir, scope=scope, owner_key=owner, **({"kinds": kinds} if kinds else {}))
        found = [entry for entry in found if not (lead and getattr(entry, "redacted", False))]
        found = sorted(found, key=lambda entry: entry.at, reverse=True)[:limit]
        rows = [{"ref": e.ref, "code": e.code, "kind": e.kind, "scope": e.scope, "title": e.title, "text": e.text,
                 "sim": 0.0} for e in found]
        states = {}
    if not rows:
        return reply(True, "没有找到相关的记忆。", count=0, items=[])
    scopes = {"global": "全局", "project": "专项", "topic": "话题"}
    lines = [f"{row['code']} · {KIND_WORDS.get(row['kind'], row['kind'])} · {scopes.get(row['scope'], row['scope'])} · "
             f"{' '.join(str(row['text']).split())[:SEARCH_TEXT]}{STATE_WORDS.get(states.get(row['ref'], ''), '')}"
             for row in rows]
    items = [{k: row.get(k) for k in ("code", "ref", "kind", "scope", "title", "sim")} for row in rows]
    return reply(True, "\n".join(lines), count=len(rows), items=items)


TOOLS = {"knowledge_save": knowledge_save, "knowledge_update": knowledge_update, "knowledge_drop": knowledge_drop,
         "knowledge_search": knowledge_search, "history_search": history_search}
