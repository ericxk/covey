"""What the butler or a project's lead hands a topic of the owner's memory (docs/butler.md 派活时挑带): the short codes in the
``memory`` of topic_start, topic_send, topic_fork, project_topic_start and project_topic_send.

The butler and the lead read 全局 的「相关时才带」 whole each turn (app/memory_turn.py pool: the 全局 资料); a topic reads only
what its search finds of it, and what one of them picked for it here. ``check`` takes a call's codes: each must name an
entry in force, a 资料 (a rule the topic carries already, a decision the Bridge carries while it is in progress), of 全局 or of
the lead's own project, at most ``MAX`` of them and ``CHARS``
characters with what the topic carries already; each one refused is said with why, the others go on. ``remember`` writes
the ones taken into the topic's ``memory_carried`` ({code, ref, by}: added to, never twice), and every turn of it after
carries them whole at the tail of its message, the Bridge's own words over them (app/memory_turn.py ``carried``,
app/prompts.py memory_tail_block): what a lead wrote in a task is never one. Standard library and core/ only."""
from __future__ import annotations

from covey.core import memstore

KEY = "memory_carried"  # topic.json: [{code, ref, by}], the oldest first.
MAX = 5  # Entries a topic carries this way, and a call may name.
CHARS = 800  # Their characters together (app/memory_turn.py NOTE_CHARS, the tail's notes).
BY = ("butler", "lead")


def codes_of(raw) -> list:
    """The short codes a call's ``memory`` names (a list, or one string of them), each once, in its order."""
    if isinstance(raw, str):
        raw = raw.replace("，", ",").replace("、", ",").split(",")
    if not isinstance(raw, (list, tuple)):
        return []
    found = []
    for item in raw:
        code = str(item or "").strip()
        if code and code.upper() not in (seen.upper() for seen in found):
            found.append(code)
    return found


def _why_not(entry, code, *, project) -> str:
    if entry is None:
        return f"{code}：找不到这条记忆"
    if entry.status != "active":
        return f"{code}：已经不在用了（作废、收起或办完）"
    if entry.kind == "rule":
        return f"{code}：是规矩，话题每一轮本来就带"
    if entry.kind != "note":
        return f"{code}：是主人答过的题，在办的话题 Bridge 自己会带"
    if entry.scope == "project" and project and entry.owner_key == project:
        pass
    elif entry.scope != "global":
        return f"{code}：只能带全局的或本专项的"
    return ""


def check(state_dir, raw, *, project="", carried=()) -> tuple[list, list]:
    """(the entries taken, the refusals: a line each, its code first) of a call's ``memory`` ``raw`` for a topic in
    ``project`` (the lead's, '' for the butler's: 全局 only); ``carried``: what the topic
    carries already (``entries_of``), counted in ``MAX`` and ``CHARS``, and not taken again."""
    taken, refused = [], []
    have = {entry.ref for entry, _ in carried}
    count, used = len(carried), sum(len(entry.text) for entry, _ in carried)
    for code in codes_of(raw):
        try:
            entry = memstore.find(state_dir, code)
        except (OSError, ValueError):
            entry = None
        why = _why_not(entry, code, project=project)
        if why:
            refused.append(why)
            continue
        if entry.ref in have:
            continue  # Carried already: nothing to say.
        if count + 1 > MAX:
            refused.append(f"{code}：一个话题最多带 {MAX} 条")
            continue
        if used + len(entry.text) > CHARS:
            refused.append(f"{code}：带上它就超过 {CHARS} 字")
            continue
        taken.append(entry)
        have.add(entry.ref)
        count, used = count + 1, used + len(entry.text)
    return taken, refused


def remember(data: dict, entries, by: str) -> list:
    """``entries`` added to the topic.json ``data``'s ``memory_carried`` (once each, by ``by``: butler or lead); the rows
    added. The caller holds the topic's lock and writes ``data``."""
    rows = [row for row in data.get(KEY) or () if isinstance(row, dict)]
    known = {str(row.get("ref") or "") for row in rows} | {str(row.get("code") or "") for row in rows}
    added = []
    for entry in entries:
        if entry.ref in known or (entry.code and entry.code in known):
            continue
        row = {"code": entry.code, "ref": entry.ref, "by": by if by in BY else "butler"}
        rows.append(row)
        added.append(row)
        known |= {entry.ref, entry.code}
    if added:
        data[KEY] = rows
    return added


def entries_of(state_dir, data: dict, *, project="") -> list:
    """[(Entry, by)] of the topic.json ``data``'s ``memory_carried`` a turn carries now: each found again (by its code, which
    a change of kind or scope keeps; else by its ref), still a 资料 in force, of 全局 or of ``project`` (the topic's now). One no longer so is left out, never an error."""
    out, seen = [], set()
    for row in (data or {}).get(KEY) or ():
        if not isinstance(row, dict):
            continue
        entry = None
        for key in (row.get("code"), row.get("ref")):
            if key:
                try:
                    entry = memstore.find(state_dir, str(key))
                except (OSError, ValueError):
                    entry = None
                if entry is not None:
                    break
        if entry is None or entry.ref in seen or _why_not(entry, entry.code, project=project):
            continue
        seen.add(entry.ref)
        out.append((entry, "lead" if row.get("by") == "lead" else "butler"))
    return out


def said(taken, refused, *, steered=False) -> str:
    """What the call's answer adds (" …。", after a sentence that ended): the codes carried from now on (``steered``: from its
    next turn, the one running has its message already), and each refused with why; '' with neither."""
    parts = []
    if taken:
        when = "从它的下一轮起" if steered else ""
        parts.append("带上了记忆 " + "、".join(entry.code for entry in taken) + f"（{when}按原文放在每一轮消息的末尾）")
    if refused:
        parts.append("没带上的记忆：" + "；".join(refused) + "；其余照常")
    return (" " + "。".join(parts) + "。") if parts else ""
