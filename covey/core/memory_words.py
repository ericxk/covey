"""What the owner reads where a reply names a memory short code: the entry's title, never the code (docs/butler.md 记忆).

The rules tell every turn to say a rule or a decision in words and keep the short codes (core/memstore.py CODE_RE) for the
tools and the checkpoint; a long session still writes "（按 DX9EA）" or "带上 R6M64、R9DA6". ``shown`` is the last step
before such words reach the owner, at the outlets that draw them (a turn's task card and answer card, its relay at the
private chat's top level, a lead's report, a question card, the web page). What every record keeps is the model's own words
(a turn's ``final_text``, its saved reply and result file: app/turn_delivery.py), for 排查、重投 and 审计.

- Only a code of an entry the store has (memory/codes.json) is changed, and only as a word of its own: not inside a fenced
  code block, a Markdown link, an address or an identifier (a path, a commit, ``a.R6M64``); inline code only when all it
  holds is such codes (`` `DX9EA` ``, `` `R6M64`、`R9DA6` ``). Any other five capitals are left as they are.
- A code that only points at its entry inside brackets ("（按 DX9EA）", "（`DGP9S`）", "（见 R9DA6，带上测试）") goes with
  what points (the brackets too when nothing else is in them); one the words stand on ("`R9DA6` 维持不拆", "带上 R6M64")
  becomes the entry's title in 「」 (a decision's answer, without 主人答：), a link to the entry on the web page's 记忆 with
  ``url`` (a rule or a note of 全局 or a project in force: a decision is no entry of the page's lists).
- Nothing with the memory off (COVEY_MEMORY=0): the words as they were.
- ``for_chat``: only in the owner's private chat (owner-chat.json). A 代答 topic's words go to other people as the owner's,
  and a group's are read by others: the codes there stay as they are, so no title of the owner's memory is shown there.

The titles are read through core/memstore.py ``find`` for the codes a text names, once each while the memory's sentinel
(memory/.rev) and the code table stay as they are: nothing walks the store, and nothing is read under a record's lock.
Standard library and core only; never raises."""
from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple

from covey.core import memory, memstore, web_sync
from covey.core.state import read_object

OWNER_CHAT = "owner-chat.json"  # app/access.py OWNER_CHAT: the owner's private chat with the bot.
SHOWN_CHARS = 32  # Characters of a title shown in place of its code.
ANSWER_MIN = 4  # A decision's answer shorter than this ("是", "A") is shown after its question.
TEXT_MAX = 200_000  # A longer text is left as it is.
KEPT = 2000  # Codes kept per state directory between two changes of the memory.

_CODE = r"[RDN][0-9A-HJKMNP-TV-Z]{4,6}"  # core/memstore.py CODE_RE
_ANY = re.compile(_CODE)
_HOLD, _HELD_END = "\ue000", "\ue001"  # Private use: a held part (code, link, address) while the words around it change.
_HELD = re.compile(_HOLD + r"(\d+)" + _HELD_END)
_PROTECT = re.compile(
    r"(?P<fence>^[ \t]*(?P<mark>`{3,}|~{3,}).*?(?:\n[ \t]*(?P=mark)[`~]*[ \t]*(?=\n|\Z)|\Z))"
    r"|(?P<inline>`[^`\n]+`)"
    r"|(?P<link>!?\[[^\]\n]*\]\([^)\n]*\))"
    r"|(?P<url><?(?:https?|ftp)://[^\s<>）]+>?)",
    re.S | re.M)
# A code as a word of its own: not after or before what makes it part of a path, an address, an identifier or a quote of code.
_NEAR = r"0-9A-Za-z_/\\\-#=?&@%$+~\"'"
_BARE = re.compile(rf"(?P<pre>[ \t]*)(?<![{_NEAR}.:])(?P<code>{_CODE})(?![{_NEAR}])(?!:\S)(?!\.[0-9A-Za-z])"
                   r"(?P<post>[ \t]*(?:[：·—][ \t]*)?)")
_WORD = re.compile(rf"(?<![{_NEAR}.:])({_CODE})(?![{_NEAR}])(?!:\S)(?!\.[0-9A-Za-z])")
_PAREN = re.compile(r"(?P<sp>[ \t]?)(?P<open>[（(])(?P<body>[^（）()\n\ue000\ue001]{1,160})(?P<close>[）)])")
_PARTS = re.compile(r"([，,；;])")
# What points at an entry beside its code inside brackets: "按 DX9EA", "见 R9DA6、RFVZY", "记忆 R6M64".
_POINTS = re.compile(r"按照|按|参见|详见|另见|见|依据|依照|根据|据|照|即|对应|记忆短码|短码|编号|记忆|规矩|决定|资料|条目|"
                     r"主人答|你答过|你定的|主人定的|以及|和|与|及|[、/:：·\s]")
_TITLED = "「“『《"  # A code followed by one of these is followed by its title already.
_OPENED = {"「": "」", "“": "”", "『": "』"}
_RECOMMENDED = re.compile(r"\s*[（(]推荐[）)]\s*$")


class Shown(NamedTuple):
    title: str  # What stands in place of the code.
    linkable: bool  # Whether the web page's 记忆 opens it (``?code=``).
    starts: tuple  # How words that give its title begin (its title's and its text's first characters).


_seen: dict = {}  # state directory -> (stamp, {code: Shown | None})
_owner: dict = {}  # owner-chat.json path -> (stat stamp, chat id)


def _stamp(state_dir) -> tuple:
    out = []
    for path in (memstore.rev_path(state_dir), memstore.codes_path(state_dir)):
        try:
            stat = path.stat()
            out.append((stat.st_mtime_ns, stat.st_size))
        except (OSError, TypeError):
            out.append(None)
    return tuple(out)


def _table(state_dir) -> dict:
    key, stamp = str(state_dir), _stamp(state_dir)
    kept = _seen.get(key)
    if kept is None or kept[0] != stamp or len(kept[1]) > KEPT:
        kept = (stamp, {})
        _seen[key] = kept
    return kept[1]


def clip(words: str) -> str:
    words = " ".join(str(words or "").split()).rstrip("…").strip()
    return words if len(words) <= SHOWN_CHARS else words[:SHOWN_CHARS - 1].rstrip() + "…"


def title_of(entry) -> str:
    """The words the owner reads for ``entry`` (core/memstore.py Entry): a rule's or a note's title; a decision's answer, or its
    question with the answer when that is short, without 主人答： and （推荐）. At most SHOWN_CHARS characters."""
    if entry.kind == "decision":
        words = " ".join(str(entry.text or "").split())
        words = words[len("主人答："):] if words.startswith("主人答：") else words
        asked, arrow, answer = words.partition("→")
        answer = _RECOMMENDED.sub("", answer).strip()
        words = answer if len(answer) >= ANSWER_MIN else (f"{asked.strip()}→{answer}" if arrow and answer else asked)
        return words.strip() or str(entry.title or "")
    return str(entry.title or "") or memstore.auto_title(entry.text)


def _resolve(state_dir, code: str, table: dict, *, inner: bool = True) -> Shown | None:
    if code in table:
        return table[code]
    found = None
    try:
        entry = memstore.find(state_dir, code) if code in memstore.codes(state_dir)["codes"] else None
    except Exception:  # noqa: BLE001 - an unreadable store changes nothing
        entry = None
    if entry is not None and str(entry.code or "").upper() == code:
        words = title_of(entry)
        if inner:  # A decision's words name other entries: their titles in 『』, one level only.
            words = _bare(words, lambda other: _resolve(state_dir, other, table, inner=False), "", quotes="『』")
        words = clip(words)
        starts = tuple(start for start in (clip(entry.title)[:6], " ".join(str(entry.text or "").split())[:6]) if len(start) >= 4)
        linkable = entry.kind in ("rule", "note") and entry.status == "active" and entry.scope in ("global", "project")
        found = Shown(words, linkable, starts) if words else None
    if inner:
        table[code] = found
    return found


def memory_url() -> str:
    """The web page's 记忆 (``page_url`` + /memory, as cards/memory_panel.py center_url makes it), '' without the page."""
    url = web_sync.page_url()
    if not url or "?" in url or "#" in url:
        return ""
    return url.rstrip("/") + "/memory"


def owner_chat(state_dir) -> str:
    """The owner's private chat with the bot as owner-chat.json says ('' without it), read again only when its stat changed."""
    path = Path(state_dir) / OWNER_CHAT
    try:
        stat = path.stat()
        stamp = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
    except (OSError, TypeError):
        return ""
    kept = _owner.get(str(path))
    if kept is not None and kept[0] == stamp:
        return kept[1]
    try:
        chat = read_object(path).get("chat_id")
    except Exception:  # noqa: BLE001 - not known: nothing is changed
        return ""
    chat = chat if isinstance(chat, str) else ""
    _owner[str(path)] = (stamp, chat)
    return chat


def _cjk(char: str) -> bool:
    return bool(char) and ord(char) >= 0x2E80


def _points_only(segment: str, real) -> bool:
    """Whether a part of a bracket's words only points at entries: real codes, with what points at them and nothing else."""
    rest = _WORD.sub(lambda m: "" if real(m.group(1)) else m.group(0), segment)
    return rest != segment and not _POINTS.sub("", rest).strip()


def _brackets(work: str, real) -> str:
    def one(match):
        body = match.group("body")
        if not any(real(code) for code in _WORD.findall(body)):
            return match.group(0)
        parts = _PARTS.split(body)
        segments, marks = parts[0::2], parts[1::2]
        kept = [i for i, segment in enumerate(segments) if not _points_only(segment, real)]
        if not kept:
            return ""  # The brackets only pointed: they go, with the space before them.
        if len(kept) == len(segments):
            return match.group(0)
        words = segments[kept[0]].strip()
        for previous, i in zip(kept, kept[1:]):
            words += marks[previous] + segments[i].strip()
        return match.group("sp") + match.group("open") + words + match.group("close")
    return _PAREN.sub(one, work)


def _bare(work: str, resolve, url: str, *, quotes: str = "「」") -> str:
    def one(match):
        code = match.group("code")
        shown = resolve(code)
        if shown is None:
            return match.group(0)
        text, pre, post = match.string, match.group("pre"), match.group("post")
        before = text[match.start() - 1] if match.start() else ""
        after = text[match.end()] if match.end() < len(text) else ""
        keep_pre = pre if pre and not _cjk(before) else ""
        rest = text[match.end():match.end() + 40]
        if (after and after in _TITLED and not post.strip()) or any(rest.startswith(start) for start in shown.starts):
            return keep_pre  # The title follows already: the code alone goes.
        if not pre and before in _OPENED and text[match.end():match.end() + 1] == _OPENED[before]:
            return shown.title  # 「R6M64」: the brackets are there.
        core = quotes[0] + shown.title + quotes[1]
        if url and shown.linkable:
            core = f"[{core}]({url}?code={code})"
        keep_post = post if post.strip() or (post and not _cjk(after)) else ""
        return keep_pre + core + keep_post
    return _BARE.sub(one, work)


def shown(text, state_dir, *, url: str = "", inline: bool = True) -> str:
    """``text`` as the owner reads it (the module's docstring): each real short code that stands as a word of its own out of
    code and links, inside brackets only pointing at its entry left out, else its entry's title; ``url``: the web page's 记忆
    (``memory_url``), whose ``?code=`` the title links to for a rule or a note of 全局 or a project in force ('' plain words).
    ``inline`` False: inline code is code even when it holds only codes (a card's Markdown, where a one-line fence was made
    inline code: feishu/message_request.py optimize_markdown). ``text`` as it is when it names no code of the store, or on
    any error."""
    if not isinstance(text, str) or not text or state_dir is None or len(text) > TEXT_MAX or not _ANY.search(text) \
            or _HOLD in text or not memory.enabled():
        return text
    try:
        table, unwrap = _table(state_dir), inline

        def resolve(code):
            return _resolve(state_dir, code, table)

        def real(code):
            return resolve(code) is not None

        held = []

        def hold(match):
            inline = match.group("inline") if unwrap else None
            if inline:
                inner = inline[1:-1]
                codes = _WORD.findall(inner)
                if codes and all(real(code) for code in codes) and not re.sub(r"[、，,;；\s和与及]|" + _CODE, "", inner):
                    return inner  # `DX9EA`, `R6M64`、`R9DA6`: codes as words, not code.
            held.append(match.group(0))
            return f"{_HOLD}{len(held) - 1}{_HELD_END}"

        work = _PROTECT.sub(hold, text)
        if not _ANY.search(_HELD.sub("", work)):
            return text
        work = _bare(_brackets(work, real), resolve, url)
        return _HELD.sub(lambda m: held[int(m.group(1))], work)
    except Exception:  # noqa: BLE001 - the words as the model wrote them
        return text


def for_chat(text, state_dir, chat_id, *, url=None, inline: bool = True) -> str:
    """``shown`` for words that go to ``chat_id``, only when it is the owner's private chat (``owner_chat``): elsewhere (a
    代答 topic's group, any other group) ``text`` as it is. ``url`` None: the web page's 记忆 when there is one."""
    if not isinstance(text, str) or not text or not chat_id or state_dir is None or not _ANY.search(text):
        return text
    if str(chat_id) != owner_chat(state_dir):
        return text
    return shown(text, state_dir, url=memory_url() if url is None else url, inline=inline)


QUESTION_WORDS = ("header", "question", "context")  # A question's words a card shows; ``id`` and options' ``label`` never.
TRIAGE_WORDS = ("why", "context", "recommend", "if_not")  # What the lead hands with an escalated question (core/triage.py).


def question_view(record, state_dir, chat_id=None) -> dict:
    """A copy of the question ``record`` with the words its cards show (``QUESTION_WORDS``, each option's ``description``, the
    lead's notes ``TRIAGE_WORDS`` and ``mirror_output``) as ``shown`` makes them, plain words (no links: some are plain text);
    ``chat_id`` None: the record's own. The ids and the options' labels stay as they are: an answer is matched by them
    (app/answers.py). ``record`` itself when nothing changes, or when it is not the owner's private chat's."""
    if not isinstance(record, dict) or state_dir is None:
        return record
    chat = record.get("chat_id") if chat_id is None else chat_id
    if not chat or str(chat) != owner_chat(state_dir):
        return record

    def clean(value):
        return shown(value, state_dir) if isinstance(value, str) else value

    try:
        changed = False
        questions = []
        for q in record.get("questions") or ():
            if not isinstance(q, dict):
                questions.append(q)
                continue
            made = {**q, **{key: clean(q[key]) for key in QUESTION_WORDS if key in q}}
            if isinstance(q.get("options"), list):
                made["options"] = [{**o, "description": clean(o["description"])} if isinstance(o, dict) and "description" in o
                                   else o for o in q["options"]]
            changed = changed or made != q
            questions.append(made)
        out = {**record, "questions": questions} if "questions" in record else dict(record)
        for key in ("mirror_output",):
            if isinstance(record.get(key), str):
                out[key] = clean(record[key])
        found = record.get("triage")
        if isinstance(found, dict):
            out["triage"] = {**found, **{key: clean(found[key]) for key in TRIAGE_WORDS if key in found}}
        return out if changed or out != record else record
    except Exception:  # noqa: BLE001 - the record as it is
        return record
