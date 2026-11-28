"""What the owner said in a topic's turn that holds from now on becomes memory (COVEY_MEMORY=1, docs/butler.md 记忆), and a
click takes it back.

The model may write ``memory`` ([{quote, text}], at most ``PER_TURN``) and ``memory_void`` ([{id, quote}]) into its checkpoint on a
turn whose rules said so (app/memory_turn.py fix_rule: the owner's words hold 以后 / 每次 / 别再 …). The Bridge, not the task
runner (rule 8: a task process changes only its own task's card and record), checks them here once the runner has exited and the
record is settled (app/detached.py finish_detached, app/job_dispatch.py for a run in this process): ``collect``.

- A ``quote`` must be a run of the owner's own words of that turn (app/memory_turn.py ``owner_words``: the job record's ``user_text``, but of an
  answer's turn only what he answered and added, never the question's own words); one that is not drops its line.
- A ``memory`` line goes through the write gate (core/memory_gate.py: a time that stops being true or a live state is refused,
  logged, not written) and becomes a rule the owner said (core/memstore.py add, ``said`` owner) in the narrowest place there is:
  the project the topic belongs to, else the topic; 全局 only when the item says ``scope: global`` and the owner's quote says 全局.
  A topic with redaction on (or whose setting cannot be read) marks it, and a marked line never goes 全局.
- A ``memory_void`` id is a short code, a ref or an older M number of a turn (core/memstore.py find_in_turn) of an entry the turn
  carried that was not said by the owner; it becomes a tombstone (memstore.void: the text stays, nothing is lost). What the owner said
  himself (a correction of his included) is never voided this way.
- The record gets ``memory_notes`` [{kind: remembered|voided|undone, text, scope, entry_id, ref?, redacted?}] and the turn's answer cards
  a line each (cards/answer_card.py memory_element); the 不对 on a "记下了" line takes that line back (``undo``): the entry becomes
  a tombstone, the note's kind ``undone``. 升为全局 beside it (``promote``, T647 E4: what he said in one topic held only there)
  moves the line to 全局 (memstore.promote); the note says 管全局 then, its entry the new one, and 不对 takes it back.

It writes only when the turn ended well, was the owner's own in an ordinary topic (not the butler's, not a 代答 topic, not one the
butler handed on) whose words hold a trigger word, and not twice for one job. Never raises: a failure is a log line and the turn's
other ending steps go on."""
from __future__ import annotations

import time
from pathlib import Path

from covey.app import answer_cards, memory_turn
from covey.app.card_actions import action_value
from covey.app.card_lanes import after_ack
from covey.cards.answer_card import memory_element
from covey.app import memory_observe
from covey.core import memory, memory_gate, memory_usage, memstore, principles, project_memory, projects, topic_memory, turn_source
from covey.core.butler import is_butler_topic
from covey.core.checkpoint import read_checkpoint
from covey.core.jobs import find_job_record, upsert_job_record
from covey.core.topics import proxy_topic

PER_TURN = 3
ENDED_WELL = ("done", "waiting_user")
PROJECT_WORDS, TOPIC_WORDS = "管这个专项", "管这个话题"
GLOBAL_WORDS = "管全局"  # A line 升为全局 (``promote``): one of the owner's principles now.
WORKSPACE_WORDS = "管这个工作区"  # A line the memory learned as a principle of the topic's workspace (app/memory_learn.py).
PRINCIPLE_WORDS = (GLOBAL_WORDS, WORKSPACE_WORDS)  # A note whose entry is a principle: 不对 removes the principle.
SAID_GLOBAL = "全局"  # What the owner's quote says when a ``memory`` line is for every topic.
REDACTED_REFUSAL = "这条来自开着脱敏的话题，不能升为全局：全局的记忆会带进不脱敏的话题。"


def _squash(text) -> str:
    return "".join(str(text or "").split())


def from_owner(quote, said) -> bool:
    """Whether ``quote`` (not empty) is a run of ``said``, the owner's words of the turn, whitespace aside."""
    quote = _squash(quote)
    return bool(quote) and quote in _squash(said)


def reason_to_ignore(runtime, ctx, job, record) -> str:
    """Why the turn's lines are not taken; '' when they may be."""
    topic_id = str(record.get("topic_id") or getattr(job, "topic_id", "") or "")
    chat = str(record.get("chat_id") or getattr(job, "chat_id", ""))
    if record.get("status") not in ENDED_WELL:
        return "not-finished"
    if not turn_source.behavior(job, record).owner_words:
        return "not-owner-words"  # A 协作 turn's text is not the owner's (core/turn_source.py).
    if not runtime.access.owner_turn(chat, str(record.get("sender_id") or getattr(job, "sender_id", "") or "")):
        return "not-owner"
    if memory_turn.handed_on(record.get("message_id") or getattr(job, "message_id", "")):
        return "handed-on"
    if is_butler_topic(topic_id, chat):
        return "butler"
    if proxy_topic(ctx):
        return "proxy"
    if not memory_turn.triggered(memory_turn.owner_words(record, ctx.state_dir),
                                 knowledge_takes=_knowledge_takes(runtime, ctx, job, record)):
        return "no-trigger"
    return ""


def _knowledge_takes(runtime, ctx, job, record) -> bool:
    from covey.app import knowledge_turn
    return knowledge_turn.remembers(ctx, job, owner_turn=lambda: True)


def _topic_marked(ctx, topic_id) -> bool:
    """Whether a line from the topic carries the redaction mark: redaction on, or its setting cannot be read."""
    from covey.core.redaction import topic_redaction
    try:
        return bool(topic_redaction(ctx.state_dir, topic_id))
    except ValueError:
        return True


def _take(ctx, record, text, marked, quote="", *, everywhere=False, log=None):
    """(the note of one ``memory`` line, or None): the line written in the narrowest place (a rule the owner said); a project's
    keeps where it came from (the owner's message the turn answered, its topic and job, and the owner's words ``quote``d).
    ``everywhere``: the owner said 全局 (never for a marked line). The gate's refusal is logged by ``log``, never the words."""
    topic_id = str(record.get("topic_id") or "")
    state = ctx.state_dir
    found = projects.topic_project(state, topic_id) if projects.memory_enabled() else None
    if everywhere and not marked:
        scope, owner, words = "global", "", GLOBAL_WORDS
    elif found is not None and found.role == projects.MEMBER:
        scope, owner, words = "project", found.id, PROJECT_WORDS
    elif found is None:
        scope, owner, words = "topic", topic_id, TOPIC_WORDS
    else:
        return None  # A lead's own topic: the butler's tools are its way.
    text, verdict = memory_gate.accept(text, kind="rule")
    if not text:
        if log:
            log(f"memory line refused by the gate reason={verdict.reason or 'gate'} job=#{record.get('job_id')}")
        return None
    origin = {"message_id": str(record.get("message_id") or ""), "topic_key": topic_id, "job": str(record.get("key") or ""),
              "quote": str(quote or "")}
    stamp = time.time()
    entry = memstore.add(state, kind="rule", scope=scope, owner_key=owner, text=text, said="owner", by="owner",
                         origin=origin, redacted=marked, now=stamp)
    new = entry.at >= stamp - 1
    return ({"kind": "remembered", "text": entry.text, "scope": words, "entry_id": memstore.parse_ref(entry.ref)[2],
             "ref": entry.ref, **({"code": entry.code} if entry.code else {}), **({"redacted": True} if marked else {})}
            if new else None)


def _void(ctx, record, item):
    """(the note of one ``memory_void`` item, or None): an entry the turn carried that the owner did not say, struck out."""
    entry = memstore.find_in_turn(ctx.state_dir, str(item.get("id") or "").strip(), record.get("memory_ids"))
    carried = {str(row.get("ref")) for row in record.get("memory_ids") or () if isinstance(row, dict)}
    if entry is None or entry.ref not in carried or entry.status != "active" or entry.said == "owner":
        return None  # What the owner said (a correction of his included) is never voided by a model's word.
    try:
        entry = memstore.void(ctx.state_dir, entry.ref, who="memory")
    except ValueError:
        return None
    words = {"global": GLOBAL_WORDS, "project": PROJECT_WORDS}.get(entry.scope, TOPIC_WORDS)
    return {"kind": "voided", "text": entry.text, "scope": words, "entry_id": memstore.parse_ref(entry.ref)[2], "ref": entry.ref}


def collect(runtime, messenger, job, ctx) -> None:
    """Take the turn's ``memory`` and ``memory_void`` as the module says, then count what the turn used of the memory
    (app/memory_observe.py). Nothing when the switch is off."""
    if not memory.enabled():
        return
    _lines(runtime, messenger, job, ctx)
    memory_observe.observe(ctx, job, None)


def _lines(runtime, messenger, job, ctx) -> None:
    try:
        record = find_job_record(ctx.state_dir, str(getattr(job, "message_id", "") or ""))
        path = str((record or {}).get("checkpoint_path") or "")
        found = read_checkpoint(Path(path)) if path else {}
        if not record:
            return
        wanted, voids = found.get("memory"), found.get("memory_void")
        wanted, voids = wanted if isinstance(wanted, list) else [], voids if isinstance(voids, list) else []
        if not (wanted or voids) or "memory_notes" in record:
            return
        reason = reason_to_ignore(runtime, ctx, job, record)
        if reason:
            runtime.log(f"memory lines ignored reason={reason} job=#{record.get('job_id')}")
            return
        said, marked, notes = memory_turn.owner_words(record, ctx.state_dir), _topic_marked(ctx, str(record.get("topic_id") or "")), []
        for item in voids[:PER_TURN]:
            if isinstance(item, dict) and from_owner(item.get("quote"), said):
                notes.append(_void(ctx, record, item))
        for item in wanted[:PER_TURN]:
            text = project_memory.clean(item.get("text")) if isinstance(item, dict) else ""
            if text and from_owner(item.get("quote"), said):
                everywhere = item.get("scope") == "global" and SAID_GLOBAL in _squash(item.get("quote"))
                notes.append(_take(ctx, record, text, marked, item.get("quote"), everywhere=everywhere, log=runtime.log))
        notes = [note for note in notes if note]
        runtime.log(f"memory lines written={len(notes)} of {len(wanted) + len(voids)} job=#{record.get('job_id')}")
        if notes:
            upsert_job_record(ctx.state_dir, str(record["key"]), memory_notes=notes)
            show(ctx, messenger, record, notes, runtime.log)
    except Exception as exc:  # noqa: BLE001 - the turn's ending goes on; the owner's words are in the record
        runtime.log(f"memory lines failed job=#{getattr(job, 'job_id', '?')}: {type(exc).__name__}")


def show(ctx, messenger, record, notes, log) -> None:
    """The memory lines of ``notes`` put on the job's answer cards."""
    element = memory_element(notes, str(record.get("topic_id") or ""), str(record.get("job_id") or ""))
    if element is not None and messenger is not None:
        answer_cards.put_memory(ctx.state_dir, ctx.profile, messenger, str(record["key"]), element, log,
                                answer_message_id=str(record.get("answer_message_id") or ""))


def undo(host, ctx, event) -> bool:
    """不对 on a "记下了" line (``host``: the TaskControls): the entry becomes a tombstone, the note ``undone``, the lines redrawn."""
    raw = action_value(event)
    if not ctx.topic_id or raw.get("topic_id") != ctx.topic_id:
        return True
    record = find_job_record(ctx.state_dir, str(raw.get("job_id") or ""))
    entry_id = str(raw.get("entry_id") or "")
    if not record or record.get("topic_id") != ctx.topic_id or not entry_id:
        raise ValueError("找不到这条记忆所在的任务")
    notes = take_back(ctx.state_dir, ctx.topic_id, record, entry_id)
    if notes is not None:  # None: taken back already (a second click), or not this turn's.
        after_ack(lambda: show(ctx, host.messenger, record, notes, host.runtime.log))
    return True


UNDOABLE = ("remembered", "superseded", "pending")  # The kinds of a note 不对 takes back (cards/answer_card.py memory_element).


def take_back(state, topic_id, record, entry_id):
    """不对 on the entry ``entry_id`` the turn of job ``record`` (of ``topic_id``) wrote, the card's and the web page's
    (app/web_ops.py memory_undo, data contract v34): the entry a tombstone, its note ``undone`` in the job record. A line the
    learner put out of the way (``superseded``, app/memory_learn.py) is back as it was, an item it put in 待审核 (``pending``)
    is turned down. The job's notes as they are now (the caller redraws the answer card's lines from them); None when there is no such
    note still in force (taken back already, or not this turn's), nothing changed."""
    notes = [dict(note) for note in record.get("memory_notes") or ()]
    note = next((note for note in notes if note.get("kind") in UNDOABLE and note.get("entry_id") == entry_id), None)
    if note is None:
        return None
    if note["kind"] == "remembered":
        _forget(state, topic_id, entry_id, note.get("scope"), *([note["ref"]] if note.get("ref") else []))
    elif note["kind"] == "superseded":
        if note.get("project_id"):
            project_memory.unsettle(state, note["project_id"], [(entry_id, note.get("before") or {})])
        else:
            topic_memory.restore(state, topic_id, entry_id)
    elif entry_id.startswith("review:"):
        memstore.review_set(state, entry_id, "rejected", by="owner")
    # A pending line of an older release names an evening candidate: that feature is gone, the line only says 已撤销.
    note["kind"] = "undone"
    notes = [note if old.get("entry_id") == entry_id and old.get("kind") in UNDOABLE else old for old in notes]
    upsert_job_record(state, str(record["key"]), memory_notes=notes)
    return notes


def _forget(state, topic_id, entry_id, scope="", ref="") -> None:
    """The entry ``entry_id`` (``ref``, when the note has it) a turn of ``topic_id`` wrote, a tombstone (memstore.void); a note
    from before the memory had one store: a tombstone in its project's memory or the topic's, a principle (``scope`` one of
    PRINCIPLE_WORDS) removed."""
    if ref:
        try:
            memstore.void(state, ref, who="owner")
        except ValueError:
            pass  # Gone or voided already.
        return
    if scope in PRINCIPLE_WORDS:
        principles.delete(state, entry_id)
        return
    found = projects.topic_project(state, topic_id) if projects.memory_enabled() else None
    if found is not None:
        project_memory.delete(state, found.id, entry_id)
    topic_memory.delete(state, topic_id, entry_id)


def _entry(state, topic_id, entry_id, ref=""):
    """The live entry ``entry_id`` a turn of ``topic_id`` wrote, in its project's memory or the topic's; None when it is gone."""
    if ref:
        found = memstore.find(state, ref)
        return {"id": entry_id, "text": found.text, "redacted": found.redacted} if found and found.status == "active" else None
    found = projects.topic_project(state, topic_id) if projects.memory_enabled() else None
    rows = [row for row in (project_memory.stored(state, found.id) if found is not None else []) if not row.get("deleted")]
    rows = [*rows, *topic_memory.live(state, topic_id)]
    return next((row for row in rows if isinstance(row, dict) and row.get("id") == entry_id), None)


def promote(host, ctx, event) -> bool:
    """升为全局 on a "记下了" line of 管这个专项 or 管这个话题 (``host``: the TaskControls, COVEY_MEMORY=1): the line becomes one of the
    owner's principles (core/principles.py save, 通用: every topic reads it on his own turns), the entry it was a tombstone, the
    note 管全局 with the principle as its entry, the lines redrawn. ValueError says why nothing changed (a line from a topic with
    redaction on, a full group); a second click does nothing."""
    raw = action_value(event)
    if not ctx.topic_id or raw.get("topic_id") != ctx.topic_id:
        return True
    record = find_job_record(ctx.state_dir, str(raw.get("job_id") or ""))
    entry_id = str(raw.get("entry_id") or "")
    if not record or record.get("topic_id") != ctx.topic_id or not entry_id:
        raise ValueError("找不到这条记忆所在的任务")
    notes = [dict(note) for note in record.get("memory_notes") or ()]
    note = next((note for note in notes if note.get("kind") == "remembered" and note.get("entry_id") == entry_id
                 and note.get("scope") in (PROJECT_WORDS, TOPIC_WORDS)), None)
    if note is None:
        return True  # Promoted or taken back already (a second click), or not this turn's.
    entry = _entry(ctx.state_dir, ctx.topic_id, entry_id, note.get("ref") or "")
    if entry is None:
        raise ValueError("这条记忆已经不在了（在设置卡「记忆」里删掉过）")
    if note.get("redacted") or entry.get("redacted"):  # core/memory.py: only a topic with redaction on reads it.
        raise ValueError(REDACTED_REFUSAL)
    if note.get("ref"):
        moved = memstore.promote(ctx.state_dir, note["ref"], scope="global", who="owner")
        note.update(scope=GLOBAL_WORDS, entry_id=memstore.parse_ref(moved.ref)[2], ref=moved.ref)
        upsert_job_record(ctx.state_dir, str(record["key"]), memory_notes=notes)
        host.runtime.log(f"memory line promoted to global job=#{record.get('job_id')}")
        after_ack(lambda: show(ctx, host.messenger, record, notes, host.runtime.log))
        return True
    found, _ = principles.save(ctx.state_dir, principles.OWNER, note["text"], source={
        "job_key": str(record.get("key") or ""), "job_id": record.get("job_id"), "from": note.get("scope")})
    held = projects.topic_project(ctx.state_dir, ctx.topic_id) if projects.memory_enabled() else None
    memory_usage.carry(ctx.state_dir, f"project:{held.id}:{entry_id}" if held is not None else f"topic:{entry_id}", f"principle:{found['id']}")
    _forget(ctx.state_dir, ctx.topic_id, entry_id, note.get("scope"))
    note.update(scope=GLOBAL_WORDS, entry_id=found["id"])
    upsert_job_record(ctx.state_dir, str(record["key"]), memory_notes=notes)
    host.runtime.log(f"memory line promoted to a principle job=#{record.get('job_id')}")
    after_ack(lambda: show(ctx, host.messenger, record, notes, host.runtime.log))
    return True


def void_turn(runtime, messenger, ctx, record) -> int:
    """The owner recalled the message of the turn ``record`` (app/source_watch.py): every line it 记下了 is taken back as
    不对 does, its answer cards redrawn; how many."""
    notes = [dict(note) for note in record.get("memory_notes") or () if isinstance(note, dict)]
    topic, taken = str(record.get("topic_id") or ""), 0
    for note in notes:
        if note.get("kind") == "remembered" and note.get("entry_id"):
            _forget(ctx.state_dir, topic, str(note["entry_id"]), note.get("scope"), *([note["ref"]] if note.get("ref") else []))
            note["kind"], taken = "undone", taken + 1
    if taken:
        upsert_job_record(ctx.state_dir, str(record["key"]), memory_notes=notes)
        show(ctx, messenger, record, notes, runtime.log)
    return taken
