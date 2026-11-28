"""What the owner said in a topic's turn that holds from now on, or asked to be noted, becomes memory (COVEY_MEMORY=1,
docs/butler.md 记忆), and a click takes it back.

The model may write ``memory`` ([{quote, text, kind, scope}], at most ``PER_TURN``) and ``memory_void`` ([{id, quote}]) into its
checkpoint on a turn whose rules said so (app/memory_turn.py fix_rule: the owner's words hold one of core/memory.py CUES;
app/prompts.py MEMORY_FIX_RULE, the one rule that took the place of the knowledge entries' ``remember`` too, T908 M3a). The
Bridge, not the task runner (rule 8: a task process changes only its own task's card and record), checks them here once the
runner has exited and the record is settled (app/detached.py finish_detached, app/job_dispatch.py for a run in this process):
``collect``.

- A ``quote`` must be a run of the owner's own words of that turn (app/memory_proof.py prove: of his level, his turn, and
  app/memory_turn.py ``owner_words``: of an answer's turn only what he answered and added, never the question's own words);
  one that is not drops its line.
- ``take`` is the one way the owner's words of a turn reach the memory, this module's and 记忆自己长's (app/memory_learn.py):
  through core/memory_gate.py write (the gate refuses a time that stops being true or a live state, logged, not written), as
  his (``said`` owner, his message and quote the origin). ``kind`` rule (以后照做) or note (资料, carried when related). Its
  ``scope`` is the model's suggestion: ``topic`` the topic (only this matter), ``project`` the topic's project (in a project's
  topic, or its lead's, in force at once), ``global`` every topic: a note goes 全局 at once; a rule is written where it was said
  (the project, else the topic) and 待审核 asks whether it is to hold 全局. A lead's own topic has no line of its own: what the
  owner says there is the project's. A rule or a note whose words name what only he says yes to (core/memory_gate.py grants)
  is not written: it waits in 待审核 and holds once he accepts it (a note of 全局 the butler or a lead may pick for a topic,
  app/memory_picks.py, whose turns read it as his). The same words as a line of the lead's confirm that line (core/memstore.py put).
- A ``memory_void`` id is a short code, a ref or an older M number of a turn (core/memstore.py find_in_turn) of an entry the turn
  carried that was not said by the owner; it becomes a tombstone (memstore.void: the text stays, nothing is lost). What the owner said
  himself (a correction of his included) is never voided this way.
- The record gets ``memory_notes`` [{kind: remembered|voided|undone|pending, text, scope, entry_id, ref?, entry_kind?,
  review?}] and the turn's answer cards a line each (cards/answer_card.py memory_element; a note's line says 相关时才带). Of the
  two kinds of 待审核 (T908 M3b): a rule for every topic is one line, "记下了：… · 先管这个专项，要管全局吗" (its note's
  ``review`` the item's key), a learned rule that grants a 待审核 line; both have 采纳 (``accept_click``: core/memory_review.py
  decide, the line then a "记下了" of where it went) and 不对. 不对 on a "记下了" line takes that line back (``undo``): the entry
  becomes a tombstone, the note's kind ``undone``, and its 待审核 item is turned down with it; on a 待审核 line it turns the
  item down; on a line 采纳 made it voids what was accepted. 升为全局 of an older card (``promote``, T647 E4) moves the line to
  全局 (memstore.promote).
- A lead's project_remember (app/project_tools.py) puts a note of its own in the record's ``lead_notes`` ({…, by: lead}): the
  answer card says "负责人记下了：…" with the turn's other lines (``show``), and 不对 takes it back the same way.

It writes only when the turn ended well, was the owner's own in an ordinary topic (not the butler's, not a 代答 topic, not one the
butler handed on) whose words hold a trigger word, and not twice for one job. Never raises: a failure is a log line and the turn's
other ending steps go on."""
from __future__ import annotations

from pathlib import Path

from covey.app import answer_cards, memory_proof, memory_turn
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
GLOBAL_WORDS = "管全局"  # A line of 全局 (a note said so, a line 升为全局, ``promote``).
WORKSPACE_WORDS = "管这个工作区"  # A line an older release learned as a principle of the topic's workspace.
PRINCIPLE_WORDS = (GLOBAL_WORDS, WORKSPACE_WORDS)  # A note whose entry is a principle: 不对 removes the principle.
WIDER_WORDS = "要不要管全局等你采纳"  # An older release's 待审核 line of a rule held where it was said (``review`` on its line now).
GRANT_WORDS = "涉及要你点头的事，采纳后才生效"  # A learned rule that would let a turn skip asking (core/memory_gate.py GRANT).
KINDS = ("rule", "note")
SCOPES = ("topic", "project", "global")  # A line's suggested scope, narrowest first.


def from_owner(quote, said) -> bool:
    """Whether ``quote`` (not empty) is a run of ``said``, the owner's words of the turn, whitespace aside."""
    return memory_proof.said_in(quote, said)


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


def place(state_dir, topic_id, scope) -> tuple[str, str, str]:
    """(scope, owner key, its words) a line of the topic ``topic_id`` suggested for ``scope`` (``SCOPES``) is written in:
    ``topic`` the topic's own, ``project`` the topic's project when it has one; a project lead's topic has no line of its own
    (the project's), and ``global`` or no suggestion is the narrowest place there is, the project, else the topic."""
    found = projects.topic_project(state_dir, topic_id) if projects.memory_enabled() else None
    if found is not None and (scope != "topic" or found.role == projects.LEAD):
        return "project", found.id, PROJECT_WORDS
    return "topic", topic_id, TOPIC_WORDS


def take(ctx, record, text, quote="", *, kind="rule", scope="", access=None, source="checkpoint", log=None) -> list:
    """The notes of one line of the owner's words of the turn of ``record`` (the module's docstring), through
    core/memory_gate.py write: [] when his ``quote`` is not proven his (app/memory_proof.py prove, by ``access``), the gate
    refuses it (logged by ``log``, never the words) or the words were there already as his. ``kind`` rule or note; ``scope``
    the suggestion (``SCOPES``); ``source`` the log line's (checkpoint, learn)."""
    log = log or (lambda line: None)
    state, topic_id, job = ctx.state_dir, str(record.get("topic_id") or ""), record.get("job_id")
    kind = kind if kind in KINDS else "rule"
    scope = scope if scope in SCOPES else ""
    proof = memory_proof.prove(access, state, memory_gate.Proof(message_id=str(record.get("message_id") or ""),
                                                                quote=str(quote or "")),
                               job_key=str(record.get("key") or "")) if access is not None else None
    if proof is None:
        log(f"memory line dropped reason=unproven job=#{job}")
        return []
    if scope == "global" and kind == "note":
        where, owner, words = "global", "", GLOBAL_WORDS
    else:
        where, owner, words = place(state, topic_id, scope)
    origin = {"topic_key": topic_id, "job": str(record.get("key") or "")}
    number = record.get("topic_number")
    if type(number) is int:
        origin["topic_number"] = number
    try:
        written = memory_gate.write(state, kind=kind, scope=where, owner_key=owner, text=text, proof=proof, by="owner",
                                    source=source, place="topic", origin=origin, learned=True,
                                    wider="global" if scope == "global" and kind == "rule" else "")
    except ValueError as exc:
        log(f"memory line skipped job=#{job}: {exc}")
        return []
    if written.reason == memory_gate.GRANT:
        log(f"memory line waits for the owner reason=grant kind={written.grant} job=#{job}")
        return [{"kind": "pending", "text": text, "scope": GRANT_WORDS, "entry_id": written.review}] if written.fresh else []
    if written.entry is None:
        log(f"memory line refused by the gate reason={written.reason or 'gate'} job=#{job}")
        return []
    entry, notes = written.entry, []
    asks = written.review if written.review and written.fresh else ""  # 推断全局: held here, 待审核 asks about 全局.
    if written.done != memstore.SAME or asks:
        notes.append({"kind": "remembered", "text": entry.text, "scope": words, "entry_id": memstore.parse_ref(entry.ref)[2],
                      "ref": entry.ref, **({"code": entry.code} if entry.code else {}),
                      **({"entry_kind": "note"} if entry.kind == "note" else {}), **({"review": asks} if asks else {}),
                      **({"over": True} if _over_room(state, entry) else {})})
    return notes


def _over_room(state_dir, entry) -> bool:
    """Whether the owner's rule ``entry`` went into a scope already full: the turns carry only its title (app/memory_turn.py
    over_room), and the answer card says so (cards/answer_card.py OVER_WORDS, T908 M5)."""
    from covey.app.memory_turn import over_room

    return entry.said == "owner" and over_room(state_dir, entry)


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
    if not _lines(runtime, messenger, job, ctx):
        _lead_lines(runtime, messenger, job, ctx)
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
        said, notes = memory_turn.owner_words(record, ctx.state_dir), []
        for item in voids[:PER_TURN]:
            if isinstance(item, dict) and from_owner(item.get("quote"), said):
                notes.append(_void(ctx, record, item))
        for item in wanted[:PER_TURN]:
            text = project_memory.clean(item.get("text")) if isinstance(item, dict) else ""
            if text and from_owner(item.get("quote"), said):
                notes += take(ctx, record, text, item.get("quote"), kind=str(item.get("kind") or "rule"),
                              scope=str(item.get("scope") or ""), access=runtime.access, log=runtime.log)
        notes = [note for note in notes if note]
        runtime.log(f"memory lines written={len(notes)} of {len(wanted) + len(voids)} job=#{record.get('job_id')}")
        if notes:
            upsert_job_record(ctx.state_dir, str(record["key"]), memory_notes=notes)
            show(ctx, messenger, record, notes, runtime.log)
            return True
    except Exception as exc:  # noqa: BLE001 - the turn's ending goes on; the owner's words are in the record
        runtime.log(f"memory lines failed job=#{getattr(job, 'job_id', '?')}: {type(exc).__name__}")
    return False


def _lead_lines(runtime, messenger, job, ctx) -> None:
    """The lines of what the turn's lead wrote with project_remember (``lead_notes``), on its answer cards."""
    try:
        record = find_job_record(ctx.state_dir, str(getattr(job, "message_id", "") or ""))
        if record and record.get("lead_notes"):
            show(ctx, messenger, record, record.get("memory_notes") or [], runtime.log)
    except Exception as exc:  # noqa: BLE001 - the turn's ending goes on
        runtime.log(f"memory lead lines failed job=#{getattr(job, 'job_id', '?')}: {type(exc).__name__}")


def show(ctx, messenger, record, notes, log, lead=None) -> None:
    """The memory lines of ``notes`` and of the lead's ``lead`` (the record's ``lead_notes`` when None) put on the job's answer
    cards."""
    lead = record.get("lead_notes") if lead is None else lead
    shown = [*(notes or ()), *(note for note in lead or () if isinstance(note, dict))]
    element = memory_element(shown, str(record.get("topic_id") or ""), str(record.get("job_id") or ""))
    if element is not None and messenger is not None:
        answer_cards.put_memory(ctx.state_dir, ctx.profile, messenger, str(record["key"]), element, log,
                                answer_message_id=str(record.get("answer_message_id") or ""))


def _clicked(ctx, event):
    """(the job record, the entry id) a click on a memory line of ``ctx``'s topic names; None for one of another topic.
    ValueError when the job or the line is not there."""
    raw = action_value(event)
    if not ctx.topic_id or raw.get("topic_id") != ctx.topic_id:
        return None
    record = find_job_record(ctx.state_dir, str(raw.get("job_id") or ""))
    entry_id = str(raw.get("entry_id") or "")
    if not record or record.get("topic_id") != ctx.topic_id or not entry_id:
        raise ValueError("找不到这条记忆所在的任务")
    return record, entry_id


def undo(host, ctx, event) -> bool:
    """不对 on a "记下了" line (``host``: the TaskControls): the entry becomes a tombstone, the note ``undone``, the lines redrawn."""
    found = _clicked(ctx, event)
    if found is None:
        return True
    record, entry_id = found
    notes = take_back(ctx.state_dir, ctx.topic_id, record, entry_id)
    if notes is not None:  # None: taken back already (a second click), or not this turn's.
        fresh = find_job_record(ctx.state_dir, str(record["key"])) or record
        after_ack(lambda: show(ctx, host.messenger, fresh, fresh.get("memory_notes") or [], host.runtime.log))
    return True


def accept_click(runtime, messenger, ctx, event) -> str:
    """采纳 on a line of an answer card that waits in 待审核 (app/memory_panel.py takes the click: the 设置 card's 采纳 kind, the
    value naming the topic and the job; the owner's, checked there): the item accepted (core/memory_review.py decide), the line
    a "记下了" of where it went, the lines redrawn. The click's outcome; ValueError says why nothing changed (a line of another
    chat's job, an item decided elsewhere); a second click does nothing."""
    raw = action_value(event)
    record = find_job_record(ctx.state_dir, str(raw.get("job_id") or ""))
    entry_id = str(raw.get("entry_id") or "")
    if (not record or not entry_id or record.get("topic_id") != raw.get("topic_id")
            or str(record.get("chat_id") or "") != str(event.get("chat_id") or record.get("chat_id") or "")):
        raise ValueError("找不到这条记忆所在的任务")
    notes = adopt(ctx.state_dir, record, entry_id)
    if notes is None:
        return "memory_accept_done"  # Accepted or taken back already (a second click).
    runtime.log(f"memory line accepted from the answer card job=#{record.get('job_id')}")
    after_ack(lambda: show(ctx, messenger, record, notes, runtime.log))
    return "memory_accept"


UNDOABLE = ("remembered", "superseded", "pending")  # The kinds of a note 不对 takes back (cards/answer_card.py memory_element).


def _where_words(entry) -> str:
    return {"global": GLOBAL_WORDS, "project": PROJECT_WORDS}.get(getattr(entry, "scope", ""), TOPIC_WORDS)


def adopt(state, record, entry_id):
    """采纳 on the note ``entry_id`` of the job ``record`` that waits in 待审核 (a ``pending`` line, its entry id the item's key;
    a "记下了" line with ``review``): the item accepted as the owner's (core/memory_review.py decide), the note a "记下了" of the
    entry it wrote or moved, a line of the entry it moved away dropped (an older release's two lines). The job's notes as they
    are now; None when there is no such note still waiting (decided already, or not this turn's), nothing changed. ValueError
    when the item cannot be accepted (decided elsewhere, gone)."""
    from covey.core import memory_review

    notes = [dict(note) for note in record.get("memory_notes") or ()]
    note = next((note for note in notes if note.get("entry_id") == entry_id and (
        note.get("kind") == "pending" and entry_id.startswith("review:") or note.get("kind") == "remembered" and note.get("review"))),
        None)
    if note is None:
        return None
    key = entry_id if note["kind"] == "pending" else str(note["review"])
    item = next((row for row in memstore.review_items(state, states=None) if row.get("key") == key), {})
    if memstore.review_state(item) != "open":
        note.pop("review", None)  # Decided elsewhere (the 设置 card, the page): the line no longer asks.
        upsert_job_record(state, str(record["key"]), memory_notes=notes)
        raise ValueError("这条待审核已经处理过了（或不在了）")
    done = memory_review.decide(state, key, "accept", who="owner")
    entry = memstore.find(state, done["refs"][0]) if done["refs"] else None
    moved = set(item.get("items") or ())
    note.pop("review", None)
    if entry is not None:
        note.update(kind="remembered", text=entry.text, scope=_where_words(entry), entry_id=memstore.parse_ref(entry.ref)[2],
                    ref=entry.ref, **({"code": entry.code} if entry.code else {}))
    notes = [old for old in notes if old is note or not (old.get("kind") == "remembered" and old.get("ref") in moved)]
    upsert_job_record(state, str(record["key"]), memory_notes=notes)
    return notes


def take_back(state, topic_id, record, entry_id):
    """不对 on the entry ``entry_id`` the turn of job ``record`` (of ``topic_id``) wrote, the card's and the web page's
    (app/web_ops.py memory_undo, data contract v34): the entry a tombstone, its note ``undone`` in the job record, and the item
    of 待审核 its line asks (``review``) turned down with it. A line the learner put out of the way (``superseded``,
    app/memory_learn.py) is back as it was, an item it put in 待审核 (``pending``) is turned down. A line of the lead's
    (``lead_notes``) is taken back the same way. The job's notes (``memory_notes``) as they are now (the caller redraws the
    answer card's lines from the record); None when there is no such note still in force (taken back already, or not this
    turn's), nothing changed."""
    for field in ("memory_notes", "lead_notes"):
        notes = [dict(note) for note in record.get(field) or () if isinstance(note, dict)]
        note = next((note for note in notes if note.get("kind") in UNDOABLE and note.get("entry_id") == entry_id), None)
        if note is not None:
            break
    else:
        return None
    if note.get("review"):  # Its 待审核 goes with it: the owner said 不对 to the rule itself.
        if memstore.review_state(next((row for row in memstore.review_items(state, states=None)
                                       if row.get("key") == note["review"]), {})) == "open":
            memstore.review_set(state, str(note["review"]), "rejected", by="owner")
    if note["kind"] == "remembered":
        _forget(state, topic_id, entry_id, note.get("scope"), *([note["ref"]] if note.get("ref") else []))
    elif note["kind"] == "superseded":
        ref = f"project:{note['project_id']}:{entry_id}" if note.get("project_id") else f"topic:{topic_id}:{entry_id}"
        was = memstore.find(state, ref)
        if note.get("project_id"):
            project_memory.unsettle(state, note["project_id"], [(entry_id, note.get("before") or {})])
        else:
            topic_memory.restore(state, topic_id, entry_id)
        memstore.noted(state, was, ref, who="owner", action="restore")
    elif entry_id.startswith("review:"):
        if memstore.review_state(next((row for row in memstore.review_items(state, states=None)
                                       if row.get("key") == entry_id), {})) == "open":
            memstore.review_set(state, entry_id, "rejected", by="owner")
    # A pending line of an older release names an evening candidate: that feature is gone, the line only says 已撤销.
    note["kind"] = "undone"
    note.pop("review", None)
    notes = [note if old.get("entry_id") == entry_id and old.get("kind") in UNDOABLE else old for old in notes]
    upsert_job_record(state, str(record["key"]), **{field: notes})
    if field == "memory_notes":
        return notes
    return [dict(note) for note in record.get("memory_notes") or () if isinstance(note, dict)]


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
        return {"id": entry_id, "text": found.text} if found and found.status == "active" else None
    found = projects.topic_project(state, topic_id) if projects.memory_enabled() else None
    rows = [row for row in (project_memory.stored(state, found.id) if found is not None else []) if not row.get("deleted")]
    rows = [*rows, *topic_memory.live(state, topic_id)]
    return next((row for row in rows if isinstance(row, dict) and row.get("id") == entry_id), None)


def promote(host, ctx, event) -> bool:
    """升为全局 on a "记下了" line of 管这个专项 or 管这个话题 (``host``: the TaskControls, COVEY_MEMORY=1): the line becomes one of the
    owner's principles (core/principles.py save, 通用: every topic reads it on his own turns), the entry it was a tombstone, the
    note 管全局 with the principle as its entry, the lines redrawn. ValueError says why nothing changed (a full group); a second click does nothing."""
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
