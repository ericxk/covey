"""查记忆 (docs/butler.md 记忆): every entry of the memory store (core/memstore.py) looked up by its words, ranked the one way
the turn's block, the tools and the command line all use: ``bin/covey_memory.py search --state <dir> --topic <id> [--owner]
[--redacted] "<一句话>"``, ``show <短码|ref>`` (the whole entry; counted as the ``show`` signal of core/memory_rank.py) and
``list``. Reading only, but for that signal.

How the words are cut (``terms``): a run of Latin letters or digits is one word, kept whole when it holds a hyphen, a dot, a
slash or an underscore (``covey-internal``, ``ep-xxx``) and also cut at them; a single letter or digit counts only next to
Chinese (``选 F``, ``第 3 次``); a run of Chinese gives every two and every three characters, but for the two-character words
any question has (``STOP_BIGRAMS``: 要不、能不、什么…); a run of one Chinese character is that character. The query alone is
widened by a few groups of words that mean the same (``SYNONYMS``, at half weight), never the entries.

How it scores (BM25F over three fields: the title twice, the text once, the repos and the device one and a half): a word only
the query has counts neither for nor against (no entry has it); ``sim`` is absolute, the entry's score against the query's own
(core/memory_rank.py similarity), so one weak hit stays weak however poor the rest. A hit needs ``covered`` too (two different
words in common, or one rare one). The order is core/memory_rank.py rank of ``sim`` times the scope's weight (SCOPE_WEIGHTS:
the topic's own, its project's, 全局, another project's, one done or put away) with the entry's use (core/memory_usage.py
counts). Decisions of the same question are listed once. Who reads what: a 代答 topic nothing; 全局 entries only on the owner's
turn (``owner``); one from a topic with redaction on only with ``redacted``; 待审核 and void entries never. A turn's lookup
(``turn_context``, and so the command's ``search``, ``show`` and ``list``) reads 代答 and redaction off the topic's own record,
never off what the command line says (``--redacted`` is read past when ``--topic`` names a topic); ``show`` without
``--owner`` reads only what ``search`` would (``_allowed``: the turn's own topic and project). Known limit (T908 M5): ``--owner``
is still the command line's word, held to the owner's own turn by the turn's rules alone: a token of the Bridge's that proves
it would be a new concept, not this release's.

``DEFAULT_TAU`` (a hit worth showing) and ``DEFAULT_TAU_DUP`` (a question answered before, an entry written twice) were set on
the private blind set of real queries (scripts/memory_eval.py prints the same figures for any set). The index of a state
directory is kept in the process while the memory's sentinel (``memory/.rev``) and the projects' folder stay as they were; the
use counts it ranks by (``memory/usage.json``, written after nearly every turn) are read on their own (``usage_of``), so a
turn's use no longer builds the index anew. Standard library and core only."""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from covey.core import memory_rank, memory_usage, memstore, projects

LIMIT = 8  # Lines the command lists unless told.
TEXT = 300  # Characters of a line printed.
DEFAULT_TAU = 0.3
DEFAULT_TAU_DUP = 0.4
SEARCHED = ("active", "done", "archived")  # What a lookup finds; 待审核 (pending) and void never.
FIELD_WEIGHTS = {"title": 2.0, "text": 1.0, "tags": 1.5}
K1, B = 1.2, 0.75
SYNONYM_WEIGHT = 0.5
SELF_LENGTH = "own"  # The query's own score at its own length ("own") or at the corpus's average one ("avg").
TRIGRAM_WEIGHT = 0.5  # A query's three-character words, against its two-character ones (1).
UNKNOWN_SHARE = 0.0  # A query word no entry has weighs this share of the rarest word's idf in the query's own score.
KIND_WORDS = {"rule": "规矩", "decision": "决定", "note": "资料"}
SCOPE_WORDS = {"global": "全局", "project": "专项", "topic": "话题"}
STATUS_WORDS = {"done": "办完了", "archived": "收起了"}

_LATIN = r"[a-z0-9]+(?:[-_./][a-z0-9]+)*"
_HAN = r"[㐀-鿿豈-﫿]"
_RUN = re.compile(rf"{_LATIN}|{_HAN}+")
_HAN_CHAR = re.compile(_HAN)
_SEP = re.compile(r"[-_./]")
STOP_BIGRAMS = frozenset((
    "要不", "不要", "能不", "不能", "是不", "不是", "会不", "不会", "用不", "不用", "有没", "没有", "可不", "不可",
    "什么", "怎么", "怎样", "为什", "如何", "哪些", "哪个", "多少", "是否", "一下", "一个", "一些", "这个", "那个",
    "这些", "那些", "这样", "那样", "这里", "那里", "可以", "需要", "应该", "还是", "就是", "已经", "如果", "因为",
    "所以", "然后", "或者", "但是", "而且", "还有", "进行", "的话", "时候", "我们", "你们", "他们", "自己", "现在",
    "以后", "之前", "之后", "的时", "了吗", "的吗", "吗？", "要先", "先要", "还要", "就要", "要是", "一直", "直接",
))
SYNONYMS = (  # Same meaning, said either way: a query with one finds the others at SYNONYM_WEIGHT.
    ("部署", "上线", "发布", "发版"), ("验收", "自验", "验证", "截图"), ("删除", "清理", "移除", "下掉"),
    ("合入", "合并", "合进"), ("网页", "网页工作台", "前端"), ("卡片", "飞书卡"), ("机器", "设备", "节点", "开发机", "电脑"),
    ("测试", "单测"), ("额度", "配额", "用量"), ("仓库", "代码仓"), ("规矩", "规则", "约定"), ("同事", "别人"),
    ("中文", "汉语"), ("英文", "英语"), ("提交", "commit"), ("分支", "branch"), ("并行", "同时"),
    ("问主人", "请示", "确认"), ("沙箱", "sandbox"), ("模型", "model"), ("超时", "timeout"), ("重试", "retry"),
    ("回滚", "回退"), ("文档", "docs"), ("报告", "结论"), ("搜索", "检索", "查找"), ("截图", "无头浏览器"),
    ("现网", "生产", "线上"), ("只读", "不改", "不动"), ("推送", "push"), ("合并请求", "mr", "pr"),
    ("评审", "review", "复核"), ("负责人", "lead"), ("管家", "butler"), ("话题", "topic"), ("专项", "project"),
)


def terms(text) -> list:
    """The words of ``text`` (the module's docstring), with repeats: a field's term frequencies."""
    text = str(text or "").lower()
    found = []
    for match in _RUN.finditer(text):
        run = match.group(0)
        if run.isascii():
            parts = [part for part in _SEP.split(run) if part]
            if len(parts) > 1:
                found.append(run)
            for part in parts:
                if len(part) >= 2 or (len(part) == 1 and _next_to_han(text, match.start(), match.end())):
                    found.append(part)
        elif len(run) == 1:
            found.append(run)
        else:
            pairs = [run[i:i + 2] for i in range(len(run) - 1)]
            found += [pair for pair in pairs if pair not in STOP_BIGRAMS]
            found += [run[i:i + 3] for i in range(len(run) - 2) if not (pairs[i] in STOP_BIGRAMS and pairs[i + 1] in STOP_BIGRAMS)]
    return found


def _next_to_han(text, start, end) -> bool:
    before = text[:start].rstrip()[-1:]
    after = text[end:].lstrip()[:1]
    return bool(_HAN_CHAR.match(before) or _HAN_CHAR.match(after))


def tokens(text) -> set:
    """The distinct words of ``text``."""
    return set(terms(text))


def _own_weight(word) -> float:
    return TRIGRAM_WEIGHT if len(word) == 3 and not word.isascii() else 1.0


def own_weights(query) -> dict:
    """word -> its weight among ``query``'s own words (no synonyms)."""
    return {word: _own_weight(word) for word in terms(query)}


def query_weights(query) -> dict:
    """word -> its weight in ``query``: its own words (``own_weights``), SYNONYM_WEIGHT for those its synonyms bring."""
    text = str(query or "").lower()
    wanted = own_weights(text)
    for group in SYNONYMS:
        if any(word in text for word in group):
            for word in group:
                for term in terms(word):
                    wanted.setdefault(term, SYNONYM_WEIGHT)
    return wanted


def _counts(words) -> dict:
    found = {}
    for word in words:
        found[word] = found.get(word, 0) + 1
    return found


@dataclass
class Doc:
    entry: memstore.Entry
    tf: dict  # word -> weighted frequency over the fields (FIELD_WEIGHTS)
    length: float
    project: str = ""  # The project the entry belongs to ('' global, or a topic outside any project).


@dataclass
class Index:
    docs: list
    df: dict
    avglen: float
    n: int
    stamp: tuple = ()
    usage: dict = field(default_factory=dict)
    flat: bool = False  # Every word weighs 1 (two texts compared with no corpus to weigh them by).
    _idfs: dict | None = None  # Worked out once (``idfs``).

    def idfs(self) -> dict:
        """word -> its idf, ln(1 + (N - df + 0.5) / (df + 0.5)) (1 for each when ``flat``), worked out once an index."""
        if self._idfs is None:
            self._idfs = {word: 1.0 if self.flat else math.log(1 + (self.n - df + 0.5) / (df + 0.5))
                          for word, df in self.df.items() if df}
        return self._idfs

    def idf(self, word) -> float:
        return self.idfs().get(word, 0.0)


def _fields(entry) -> dict:
    return {"title": terms(entry.title), "text": terms(f"{entry.text} {entry.why}"),
            "tags": terms(" ".join((*entry.repos, entry.device)))}


def _doc(entry, project="") -> Doc:
    tf, length = {}, 0.0
    for name, words in _fields(entry).items():
        weight = FIELD_WEIGHTS[name]
        length += weight * len(words)
        for word, count in _counts(words).items():
            tf[word] = tf.get(word, 0.0) + weight * count
    return Doc(entry, tf, length, project)


def build(entries, *, topic_projects=None, usage=None, stamp=()) -> Index:
    """The index of ``entries`` (memstore Entry); ``topic_projects`` {topic id: project id} for the topics' entries."""
    topic_projects = topic_projects or {}
    docs = []
    for entry in entries:
        if entry.scope == "project" or (entry.kind == "decision" and projects.valid_id(entry.owner_key)):
            project = entry.owner_key
        elif entry.scope == "topic" or entry.kind == "decision":
            project = topic_projects.get(entry.owner_key, "")
        else:
            project = ""
        docs.append(_doc(entry, project))
    df = {}
    for doc in docs:
        for word in doc.tf:
            df[word] = df.get(word, 0) + 1
    avglen = sum(doc.length for doc in docs) / len(docs) if docs else 1.0
    return Index(docs, df, max(avglen, 1.0), len(docs), stamp, usage or {})


def _topic_projects(state_dir) -> dict:
    found = {}
    for record in projects.records(state_dir):
        pid = record.get("id")
        if not projects.valid_id(pid):
            continue
        for topic in (record.get("lead_topic_id"), *(record.get("threads") or ())):
            if topic:
                found.setdefault(str(topic), pid)
    return found


def _stat(path) -> tuple | None:
    try:
        info = path.stat()
    except OSError:
        return None
    return info.st_mtime_ns, info.st_size


def stamp(state_dir) -> tuple:
    """What the index of ``state_dir`` was made from: the sentinel and the projects' folder, as stat says (the usage file is
    not: ``usage_of``)."""
    state = Path(state_dir)
    return tuple(_stat(path) for path in (memstore.rev_path(state), state / "projects"))


_indexes: dict = {}
_usages: dict = {}


def usage_of(state_dir) -> dict:
    """The use counts of ``state_dir``'s entries (core/memory_usage.py counts), read again only when the usage file changed."""
    key = str(Path(state_dir).resolve())
    made_from = _stat(memstore.usage_path(Path(state_dir)))
    found = _usages.get(key)
    if found is not None and found[0] == made_from and made_from is not None:
        return found[1]
    counts = memory_usage.counts(state_dir)
    _usages[key] = (made_from, counts)
    return counts


def index(state_dir) -> Index:
    """The index of every entry of ``state_dir`` (any status: the lookups choose), kept while nothing changed (the module's
    docstring), with the use counts as they are now (``usage_of``)."""
    key = str(Path(state_dir).resolve())
    made_from = stamp(state_dir)
    found = _indexes.get(key)
    if found is None or found.stamp != made_from or made_from[0] is None:
        found = build(memstore.entries(state_dir, statuses=None), topic_projects=_topic_projects(state_dir),
                      usage={}, stamp=made_from)
        _indexes[key] = found
    found.usage = usage_of(state_dir)
    return found


def _bm25(index_, wanted: dict, tf: dict, length: float) -> tuple[float, set]:
    score, shared = 0.0, set()
    norm = K1 * (1 - B + B * length / index_.avglen)
    for word, weight in wanted.items():
        idf = index_.idf(word)
        have = tf.get(word, 0.0)
        if not idf or not have:
            continue
        score += weight * idf * have * (K1 + 1) / (have + norm)
        shared.add(word)
    return score, shared


def self_score(index_, own: dict) -> float:
    """``score(q, q)``: the query scored as a document of its own (``own``: its own words and their weights, once each, in
    the text field)."""
    length = index_.avglen if SELF_LENGTH == "avg" else float(len(own))
    score = _bm25(index_, own, dict.fromkeys(own, 1.0), length)[0]
    unknown = [word for word in own if not index_.df.get(word)]
    if unknown and UNKNOWN_SHARE and index_.df:
        top = math.log(1 + (index_.n + 0.5) / 1.5)
        norm = K1 * (1 - B + B * length / index_.avglen)
        score += sum(own[word] for word in unknown) * UNKNOWN_SHARE * top * (K1 + 1) / (1 + norm)
    return score


def scope_weight(entry, project: str, topic: str, doc_project: str) -> float:
    weights = memory_rank.SCOPE_WEIGHTS
    if entry.status in ("done", "archived"):
        return weights["done"]
    if topic and entry.owner_key == topic:
        return weights["topic"]
    if project and doc_project == project:
        return weights["project"]
    if entry.scope == "global":
        return weights["global"]
    return weights["other_project"]


def _aliases(entry) -> list:
    """The entry's old refs, and of a decision read from a project's line the line's own ref (what the turns named it by)."""
    found = list(entry.aliases)
    if entry.ref.startswith("decision:"):
        _, owner, entry_id = entry.ref.split(":", 2)
        found.append(f"{'project' if projects.valid_id(owner) else 'topic'}:{owner}:{entry_id}")
    return found


def _allowed(entry, context) -> bool:
    """Whether a turn of ``context`` may read ``entry``: the owner's own turn everything; anyone else's only the turn's own
    topic's and its project's (not 全局, not another project's or topic's: what the turns read before memory_v2)."""
    if entry.status not in context["statuses"] or entry.kind not in context["kinds"]:
        return False
    if not context["owner"]:
        mine = (entry.scope == "topic" and bool(context["topic"]) and entry.owner_key == context["topic"]) or (
            entry.scope == "project" and bool(context["project"]) and entry.owner_key == context["project"])
        if not mine:
            return False
    return not (entry.redacted and not context["redacted"])


def _context(context) -> dict:
    context = dict(context or {})
    return {"project": str(context.get("project") or ""), "topic": str(context.get("topic") or ""),
            "owner": bool(context.get("owner", True)), "redacted": bool(context.get("redacted", False)),
            "kinds": tuple(context.get("kinds") or memstore.KINDS), "statuses": tuple(context.get("statuses") or SEARCHED),
            "tau": float(context["tau"]) if context.get("tau") is not None else DEFAULT_TAU,
            "proxy": bool(context.get("proxy", False)), "now": context.get("now")}


def rank_index(index_, query, context=None) -> list:
    """Every entry of ``index_`` the context may read that ``query`` reaches with ``sim`` ≥ its ``tau`` and enough words
    in common, the best first, a question's decisions once: dicts as ``eval_search`` says."""
    context = _context(context)
    if context["proxy"]:
        return []
    wanted = query_weights(query)
    own = self_score(index_, own_weights(query))
    if own <= 0:
        return []
    idf = index_.idfs()
    now = time.time() if context["now"] is None else float(context["now"])
    found = []
    for doc in index_.docs:
        entry = doc.entry
        if not _allowed(entry, context):
            continue
        score, shared = _bm25(index_, wanted, doc.tf, doc.length)
        if not score:
            continue
        sim = memory_rank.similarity(score, own)
        hit = memory_rank.covered(shared, idf)
        if sim < context["tau"] or not hit:
            continue
        used = index_.usage.get(entry.ref) or {}
        weight = scope_weight(entry, context["project"], context["topic"], doc.project)
        ranked = memory_rank.rank(sim * weight, float(used.get("h") or 0.0), int(used.get("e") or 0),
                                  memory_rank.freshness(entry.kind, entry.verified_at, now))
        found.append({"ref": entry.ref, "code": entry.code, "kind": entry.kind, "scope": entry.scope,
                      "owner_key": entry.owner_key, "project": doc.project, "status": entry.status, "said": entry.said,
                      "title": entry.title, "text": entry.text, "aliases": _aliases(entry), "at": entry.at,
                      "sim": round(sim, 4), "score": round(ranked, 4), "weight": weight, "covered": hit,
                      "shared": sorted(shared)})
    found.sort(key=lambda item: (-item["score"], -item["sim"], item["status"] != "active", -item["at"]))
    seen, out = set(), []
    for item in found:
        question = _question_of(item)
        if question and question in seen:
            continue
        seen.add(question)
        out.append(item)
    return out


def rank_many(index_, queries, context=None) -> list:
    """``rank_index`` of each of ``queries`` (a turn's title, its 话题要点, the sentences of the owner's words: one long text
    is covered by no entry), an entry once with its best: the best first, a question's decisions once."""
    best = {}
    for query in queries:
        for item in rank_index(index_, query, context):
            if item["ref"] not in best or item["score"] > best[item["ref"]]["score"]:
                best[item["ref"]] = item
    found = sorted(best.values(), key=lambda item: (-item["score"], -item["sim"], item["status"] != "active", -item["at"]))
    seen, out = set(), []
    for item in found:
        question = _question_of(item)
        if question and question in seen:
            continue
        seen.add(question)
        out.append(item)
    return out


def _question_of(item) -> str:
    if item["kind"] != "decision":
        return ""
    return f"{item['owner_key']}:{item['title']}"


def eval_search(state_dir, query: str, context: dict | None = None, limit: int = 5) -> list[dict]:
    """The entries of ``state_dir`` that ``query`` finds, the best first, at most ``limit``: each {ref, code, kind, scope,
    owner_key, project, status, said, title, text, aliases, at, sim, score, weight, covered, shared}. ``context``: project
    (the turn's project id), topic (its topic id), owner (default True: 全局 entries too), redacted, proxy (a 代答 topic:
    nothing), kinds, statuses (default SEARCHED), tau (default DEFAULT_TAU)."""
    return rank_index(index(state_dir), query, context)[:max(1, int(limit))]


def text_doc(text) -> tuple[dict, float]:
    """(word -> its count, length) of ``text`` as one field: what ``mutual_docs`` compares, worked out once a text."""
    words = terms(text)
    return {word: float(count) for word, count in _counts(words).items()}, float(len(words))


def mutual_docs(index_, a, b) -> tuple[float, bool]:
    """``mutual_similarity`` of two ``text_doc``s, the words weighed by ``index_``."""
    words_a, len_a = a
    words_b, len_b = b
    query_a, query_b = dict.fromkeys(words_a, 1.0), dict.fromkeys(words_b, 1.0)
    ab, shared = _bm25(index_, query_a, words_b, len_b)
    if not shared:
        return 0.0, False
    aa, _ = _bm25(index_, query_a, words_a, len_a)
    ba, _ = _bm25(index_, query_b, words_a, len_a)
    bb, _ = _bm25(index_, query_b, words_b, len_b)
    return round(memory_rank.mutual(ab, aa, ba, bb), 4), memory_rank.covered(shared, {word: index_.idf(word) for word in shared})


def _pair_index(a, b, state_dir=None, corpus=None) -> Index:
    if isinstance(corpus, Index):
        return corpus
    if corpus is not None:
        texts = [str(text) for text in corpus]
    elif state_dir is not None:
        return index(state_dir)
    else:
        texts = [a, b]
    entries = [memstore.Entry(ref=f"x:{i}", code="", kind="note", scope="global", owner_key="", title="", text=text,
                              status="active", said="owner", by="owner") for i, text in enumerate(texts)]
    made = build(entries)
    made.flat = corpus is None and state_dir is None  # Two texts alone: every word weighs the same.
    return made


def mutual_similarity(a: str, b: str, *, state_dir=None, corpus=None) -> tuple[float, bool]:
    """(mutual sim, covered) of two texts: the smaller of a→b and b→a (core/memory_rank.py mutual), the words weighed by the
    index of ``state_dir`` or by ``corpus`` (an Index, or texts), every word alike with neither; ``covered`` whether the
    words they share are enough (core/memory_rank.py covered)."""
    return mutual_docs(_pair_index(a, b, state_dir, corpus), text_doc(a), text_doc(b))


# ------------------------------------------------------------------------------------------------- the command line


def _topic_project(state_dir, topic_id) -> str:
    if not topic_id:
        return ""
    try:
        found = projects.topic_project(state_dir, topic_id)
    except (OSError, ValueError):
        return ""
    return found.id if found is not None else ""


def _proxy(state_dir, topic_id) -> bool:
    if not topic_id:
        return False
    try:
        from covey.core.topics import load_topic
        return bool(load_topic(Path(state_dir), topic_id).get("proxy"))
    except (OSError, ValueError):
        return False


def _redacted(state_dir, topic_id, given) -> bool:
    """Whether a lookup reads the entries from topics with redaction on: with a topic, whether its own record says it chose
    redaction (core/redaction.py topic_redaction; off when it cannot be read), whatever ``given`` (the command line's
    ``--redacted``) says; ``given`` only for a lookup of no topic (a person at a terminal)."""
    if not topic_id:
        return bool(given)
    from covey.core.redaction import topic_redaction

    try:
        return bool(topic_redaction(Path(state_dir), str(topic_id)))
    except ValueError:
        return False


def turn_context(state_dir, topic_id="", *, owner=False, redacted=False) -> dict:
    """The context of a lookup from a turn of ``topic_id``: its project, who may read what, its 代答 and its redaction read
    off its own record (``_proxy``, ``_redacted``)."""
    return {"topic": str(topic_id or ""), "project": _topic_project(state_dir, topic_id), "owner": bool(owner),
            "redacted": _redacted(state_dir, topic_id, redacted), "proxy": _proxy(state_dir, topic_id)}


def search(state_dir, query, topic_id="", *, owner=False, redacted=False, workspace="", limit=LIMIT) -> list:
    """The command's lookup: ``eval_search`` with the context of a turn of ``topic_id`` (``workspace`` is no longer a scope
    and is ignored)."""
    return eval_search(state_dir, query, turn_context(state_dir, topic_id, owner=owner, redacted=redacted), limit)


def where_of(item) -> str:
    if item["scope"] == "global":
        return "全局"
    if item.get("project"):
        return f"专项 {item['project']}" if item["scope"] != "topic" else f"话题 {item['owner_key']}"
    return f"话题 {item['owner_key']}"


def line(item) -> str:
    """One line of a lookup: "- R7K2M · 规矩 · 全局 · 你定的：正文"."""
    tags = [item.get("code") or item["ref"], KIND_WORDS.get(item["kind"], item["kind"]), where_of(item),
            "你定的" if item.get("said") == "owner" else "别人总结的"]
    if STATUS_WORDS.get(item.get("status")):
        tags.append(STATUS_WORDS[item["status"]])
    text = item["text"] if len(item["text"]) <= TEXT else item["text"][:TEXT - 1] + "…"
    return f"- {' · '.join(tags)}：{text}"


def render(rows, query) -> str:
    if not rows:
        return f"没有查到和“{query}”有关的记忆。"
    return "\n".join(line(row) for row in rows) + "\n看全文：covey_memory.py show <短码>"


def show(state_dir, key, *, topic_id="", owner=False, redacted=False) -> dict | None:
    """The whole entry ``key`` (a short code, a ref or an old id) names, as the context may read it; counted as the
    ``show`` signal (core/memory_rank.py SIGNAL_WEIGHTS). None when none is, or the turn may not read it: a 代答 topic
    nothing, one from a topic with redaction on only in such a topic (both off the topic's record, ``turn_context``), and
    without ``owner`` only what the turn's ``search`` reads (``_allowed``: its own topic's and its project's, never 全局,
    another project's or another topic's; T908 M5)."""
    entry = memstore.find(state_dir, key)
    if entry is None or entry.status in ("void", "pending"):
        return None
    context = _context({**turn_context(state_dir, topic_id, owner=owner, redacted=redacted), "statuses": SEARCHED})
    if context["proxy"] or (entry.redacted and not context["redacted"]):
        return None
    if not owner and not _allowed(entry, context):
        return None
    try:
        memory_usage.signal(state_dir, [entry.ref], "show")
    except (OSError, ValueError):
        pass  # A lookup the state cannot count is still answered.
    return memstore.as_dict(entry)


def show_text(found, key) -> str:
    if found is None:
        return f"没有 {key} 这条（或这一轮看不到它）。"
    head = " · ".join(part for part in (found.get("code") or found["ref"], KIND_WORDS.get(found["kind"], found["kind"]),
                                        SCOPE_WORDS.get(found["scope"], found["scope"]),
                                        "你定的" if found.get("said") == "owner" else "别人总结的",
                                        STATUS_WORDS.get(found.get("status"), "")) if part)
    lines = [head, f"标题：{found['title']}", f"正文：{found['text']}"]
    if found.get("why"):
        lines.append(f"原因：{found['why']}")
    if found.get("repos"):
        lines.append("适用仓库：" + "、".join(found["repos"]))
    origin = found.get("origin") or {}
    if origin.get("at"):
        lines.append("记于：" + time.strftime("%Y-%m-%d %H:%M", time.localtime(float(origin["at"]))))
    if found.get("superseded_by"):
        lines.append(f"已被 {found['superseded_by']} 替代")
    lines.append(f"ref：{found['ref']}")
    return "\n".join(lines)


def listing(state_dir, topic_id="", *, owner=False, redacted=False, limit=50) -> list:
    """The active entries a turn of ``topic_id`` may read (全局 on the owner's, its project's, its own; every one with no
    topic), the rules first, then the decisions and the notes, the newest first in each."""
    context = turn_context(state_dir, topic_id, owner=owner, redacted=redacted)
    if context["proxy"]:
        return []
    order = {kind: i for i, kind in enumerate(memstore.KINDS)}
    rows = []
    for doc in index(state_dir).docs:
        entry = doc.entry
        if entry.status != "active" or (entry.scope == "global" and not owner) or (entry.redacted and not context["redacted"]):
            continue
        if topic_id and entry.scope != "global" and entry.owner_key != topic_id and doc.project != context["project"]:
            continue
        if topic_id and not context["project"] and entry.scope != "global" and entry.owner_key != topic_id:
            continue
        rows.append({"ref": entry.ref, "code": entry.code, "kind": entry.kind, "scope": entry.scope, "owner_key": entry.owner_key,
                     "project": doc.project, "status": entry.status, "said": entry.said, "title": entry.title,
                     "text": entry.text, "at": entry.at})
    rows.sort(key=lambda row: (order.get(row["kind"], 9), -row["at"]))
    return rows[:max(1, int(limit))]


def _common(parser, *, query=False):
    parser.add_argument("--state", required=True, help="这个部署的状态目录")
    parser.add_argument("--topic", default="", help="这一轮的话题 id")
    parser.add_argument("--owner", action="store_true", help="主人自己的一轮：带上全局的")
    parser.add_argument("--redacted", action="store_true", help="开了脱敏的话题：带上来自脱敏话题的")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    if query:
        parser.add_argument("--workspace", default="", help="旧参数，已不按工作区分，忽略")
        parser.add_argument("--limit", type=int, default=LIMIT, help=f"最多列几条（默认 {LIMIT}）")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="covey_memory.py", description="查记忆：规矩、决定、资料，按一句话查、按短码看全文、列出")
    commands = parser.add_subparsers(dest="command", required=True)
    find = commands.add_parser("search", help="按一句话查")
    find.add_argument("query", help="要查的一句话")
    _common(find, query=True)
    one = commands.add_parser("show", help="按短码（如 R7K2M）或 ref 看一条的全文")
    one.add_argument("key", help="短码、ref 或旧 id")
    _common(one)
    every = commands.add_parser("list", help="列出这一轮看得到的记忆")
    _common(every, query=True)
    args = parser.parse_args(argv)
    if args.command == "show":
        found = show(args.state, args.key, topic_id=args.topic, owner=args.owner, redacted=args.redacted)
        print(json.dumps(found, ensure_ascii=False) if args.json else show_text(found, args.key))
        return 0 if found is not None else 1
    if args.command == "list":
        rows = listing(args.state, args.topic, owner=args.owner, redacted=args.redacted, limit=args.limit)
        print(json.dumps(rows, ensure_ascii=False) if args.json else ("\n".join(line(row) for row in rows) or "还没有记忆。"))
        return 0
    rows = search(args.state, args.query, args.topic, owner=args.owner, redacted=args.redacted, workspace=args.workspace,
                  limit=args.limit)
    print(json.dumps(rows, ensure_ascii=False) if args.json else render(rows, args.query))
    return 0


if __name__ == "__main__":
    sys.exit(main())
