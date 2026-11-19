"""How a butler turn ends on its task card (docs/butler.md 一次请求一张卡), for the turns whose Traits say
``answers_in_place`` (the butler's); app/worker.py TurnDelivery asks. Normal
topics never get here.

- ``answers_in_place``: a finished turn's verified, saved reply (rule 5) is the task card's body, and no answer card
  goes out. A turn that opened, forked or handed over topics (its successful topic_start, topic_fork and
  topic_send, main-thread/opened/<request>.json, app/butler_topics.py) has one line per topic as its body and the
  reply folded below. Not when the reply is not a completed one, goes as a file, is longer than the card takes, or
  a write fails, nor, with the card in the owner's message's thread (core/placement.py answer_on_card ROUTES), for
  a turn that opened no topic: the worker then sends the answer card.
- ``dropped_lines``: a turn stopped or refused lists the owner's messages it took as supplements, so the owner can
  edit and send them again; nothing is sent again by itself.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from covey.cards.elements import markdown_panel, md
from covey.cards.review import answer_lead
from covey.core.deliveries import CLOCK_SLACK, read_delivery, shown_preview
from covey.core.jobs import job_key
from covey.app import project_lead, topic_links
from covey.core import butler_opened, placement
from covey.core.butler import traits_of
from covey.core.saved_results import saved_delivery_kind
from covey.core.state import read_object
from covey.core.text import safe_text
from covey.core.topics import topic_label, topic_title

REPLY_FOLD = "main_reply_words"  # A card element id: cards already sent patch it by this name.
WAITS_START = "等你点开始"  # A topic opened with its launch card waiting for the owner (``routes``).
DROPPED = "main_dropped"  # CardKit: letters, digits and _, a letter first, at most 20.
PREVIEW = 40
OUTCOME_LEAD = 30  # Characters of the reply's lead after "✅ 已答：" (``outcome_words``).
LISTED = 10
ROUTED = "↪ "  # A slim card's topic line leads with it.


def _origin(state_dir, topic_id) -> dict:
    origin = read_object(Path(state_dir) / "topics" / topic_id / "topic.json").get("origin")
    return origin if isinstance(origin, dict) else {}


def _name(state_dir, topic_id, data=None) -> str:
    return topic_label(Path(state_dir), topic_id) or str((data or {}).get("label") or "") or "新话题"


def _title(text) -> str:
    return "「" + safe_text(str(text), 40) + "」" if text else ""


def _topic_title(state_dir, topic_id) -> str:
    """The topic's title (core/topics.py topic_title)."""
    return topic_title(Path(state_dir), topic_id)


def _data(record) -> dict:
    """The answer's data of an ``answered`` request's record ({} when it is not a dict)."""
    data = record["answer"].get("data")
    return data if isinstance(data, dict) else {}


def opened_lines(state_dir, job_key, slim=False) -> list[str]:
    """``routes``' lines."""
    return routes(state_dir, job_key, slim)


def routes(state_dir, job_key, slim=False) -> list[str]:
    """One line per topic the job ``job_key`` opened, forked or handed over, oldest first; [] when none. On a slim
    card (``slim``, app/task_card.py StreamingReply.slim) a line it opened or handed over leads with ↪, and one
    handed over names the topic's title and the butler's note to it (topic_send's ``note``, when its record
    keeps one)."""
    rows = []
    for _, record in butler_opened.by_job(state_dir, job_key):
        if not butler_opened.answered(record):
            continue
        data = _data(record)
        kind = butler_opened.kind_of(record)
        if kind == butler_opened.FORK:
            topic = str(record["fork_topic_id"])
            line = (f"从 **{_name(state_dir, str(record.get('source') or ''))}** 分叉出 **{_name(state_dir, topic, data)}**"
                    + _title(_origin(state_dir, topic).get("title")))
        elif kind == butler_opened.START and record.get("topic_id"):
            topic, plan = str(record["topic_id"]), record["plan"]
            parts = [safe_text(str(plan["workspace"]), 40)] if plan.get("workspace") else []
            parts += ["只读"] if _origin(state_dir, topic).get("readonly") is True else []
            parts += [] if plan.get("start") else [WAITS_START]
            line = (f"{ROUTED if slim else ''}开了 **{_name(state_dir, topic, data)}**{_title(record.get('title'))}"
                    + ("· " + " · ".join(parts) if parts else ""))
        elif record.get("topic_id") and slim:
            topic, note = str(record["topic_id"]), str(record.get("note") or "").strip()
            line = (f"{ROUTED}转给 **{_name(state_dir, topic, data)}**{_title(_topic_title(state_dir, topic))}"
                    + (f"：{safe_text(note, 100)}" if note else ""))
        elif record.get("topic_id"):
            topic = str(record["topic_id"])
            line = f"转给 **{_name(state_dir, topic, data)}**"
        else:
            continue
        rows.append((float(record.get("at") or 0), line))
    return [line for _, line in sorted(rows, key=lambda row: row[0])]


def outcome_words(state_dir, key, reply_text="", lead=False) -> str:
    """What the butler job ``key`` did, said on its card's status line and in the chat list's preview (V6/S13,
    2026-11-08): "↪ 开了 T444「标题」", "↪ 交给 T442 接着做", "↪ 补充给 T444（它正在跑）", "↪ 分叉出 T445「标题」",
    "↪ 拆成 2 件" for more than one topic, else "✅ 已答：" and the reply's lead; "" with neither. A project's lead's turn
    (``lead``) that opened or handed on to no thread says the note it recorded or that it finished the project first
    (app/project_lead.py outcome_line)."""
    said = []
    for _, record in butler_opened.by_job(state_dir, key):
        topic = str(butler_opened.topic_of(record) or "")
        if not topic or not butler_opened.answered(record):
            continue
        data = _data(record)
        name = _name(state_dir, topic, data)
        kind = butler_opened.kind_of(record)
        if kind == butler_opened.FORK:
            said.append(f"{ROUTED}分叉出 {name}{_title(_origin(state_dir, topic).get('title'))}")
        elif kind == butler_opened.START:
            said.append(f"{ROUTED}开了 {name}{_title(record.get('title'))}")
        elif data.get("steered") is True:
            said.append(f"{ROUTED}补充给 {name}（它正在跑）")
        else:
            said.append(f"{ROUTED}交给 {name} 接着做")
    if len(said) > 1:
        return f"{ROUTED}拆成 {len(said)} 件"
    if said:
        return said[0]
    if lead and project_lead.outcome_line(state_dir, key):
        return project_lead.outcome_line(state_dir, key)
    # The words under a heading first: "## 结论" alone says nothing of the answer.
    words = "\n".join(line for line in str(reply_text or "").splitlines() if not line.lstrip().startswith("#"))
    lead = answer_lead(words, OUTCOME_LEAD) or answer_lead(reply_text, OUTCOME_LEAD)
    return f"✅ 已答：{lead}" if lead else ""


def say_outcome(delivery) -> bool:
    """A butler turn done and delivered: its card's status line and preview say what it did (``outcome_words``),
    with the butler on (not its BUTLER_OFF topic with the butler off); a project's lead's turn too (the lead
    role, COVEY_PROJECTS), whose line names the threads it opened or the note it recorded. Best effort."""
    job, ctx, turn = delivery.job, delivery.ctx, delivery.turn
    if not getattr(job, "progress", None) or turn.status != "done":
        return False
    role = placement.ROLES.get(traits_of(job).name)
    if role not in (placement.BUTLER_ROLE, placement.LEAD_ROLE):
        return False
    try:
        if placement.layout() == placement.TOPICS:
            return False
        reply = turn.verified_delivery
        text = reply.text if reply is not None and saved_delivery_kind(reply, turn.verified_outcome) != "file" else ""
        line = outcome_words(ctx.state_dir, job_key(job.message_id, job.job_id), text, lead=role == placement.LEAD_ROLE)
        return bool(line) and delivery.cards.say_outcome(ctx, job, line)
    except Exception as exc:  # noqa: BLE001 - the card keeps its outcome line
        delivery.runtime.log(f"main thread outcome words not shown job=#{job.job_id}: {type(exc).__name__}")
        return False


def reply_fold(text, *, lead=False):
    """The model's own words under the topic lines, folded; on a slim card in its 详情 (Cards.set_task_element). A
    project's lead's turn (``lead``) says whose they are: the lead's, not the butler's."""
    return markdown_panel(REPLY_FOLD, "负责人的回答" if lead else "管家的回答", text, quiet=True)


def routed_body(lines) -> str:
    """The body of a turn that sent work to topics: its ``lines``. The grey last line the desk_topics layout added
    (结论出来后会回到这里) went with it (2026-11-09): each outcome comes to the chat as a message of its own (S3)."""
    return "\n".join(lines)


def answers_in_place(delivery, final_card, display_text) -> bool:
    """Show the turn's verified reply on its task card (``delivery``: worker TurnDelivery); whether the card took
    all of it. False leaves the answer card to the worker, as for a topic."""
    job, ctx, turn, cards = delivery.job, delivery.ctx, delivery.turn, delivery.cards
    reply, outcome = turn.verified_delivery, turn.verified_outcome
    if (not traits_of(job).answers_in_place or not job.progress or reply is None or turn.status != "done"
            or outcome.status != "completed" or cards.exceeds_stream_card_limit(final_card)):
        return False
    if saved_delivery_kind(reply, outcome) == "file":
        return False
    on_card = placement.plan(ctx.state_dir, job).row.answer_on_card
    if on_card == placement.NONE:
        # Never on its card (a project's lead, docs/ui.md 5.3): its conclusion is a message of its own at the top
        # level, which the owner is told of, not an edit of a card he may have scrolled past.
        return False
    if (on_card == placement.ROUTES
            and not routes(ctx.state_dir, job_key(job.message_id, job.job_id))):
        # Its card is in the owner's message's thread: the answer goes to the top level as a message of its own,
        # with a "你问" link (app/worker.py TurnDelivery.send); the card says the conclusion's lead.
        return False
    try:
        slim = getattr(job.progress, "slim", False)
        rows = routes(ctx.state_dir, job_key(job.message_id, job.job_id), slim)
        lines = [topic_links.linkify(line, ctx.state_dir, job.chat_id, profile=ctx.profile) for line in rows]
        body = (cards.render_stream_final(job, routed_body(lines), status=turn.status)
                if lines else final_card)
        if not cards.update_job_streaming_reply(ctx, job, body, force=True, idempotency_prefix=f"{delivery.prefix}-final",
                                                allow_replacement=False) or cards.body_cut(job):
            return False
        lead = placement.ROLES.get(traits_of(job).name) == placement.LEAD_ROLE
        if lines and not cards.set_task_element(ctx, job, reply_fold(display_text, lead=lead)):
            return False
    except Exception as exc:  # noqa: BLE001 - the answer card goes out instead
        delivery.runtime.log(f"main thread answer not on its card job=#{job.job_id}: {type(exc).__name__}")
        return False
    delivery.steering.close_task_controls(ctx, job)
    if not cards.finalize_trace(ctx, job, stopped=False):
        delivery.runtime.log("Final process panel update failed; preserving existing trace")
    return True


# The hand-off sentences of the butler's rules (app/prompts.py AREA_RULE) and their near forms, with the few words
# around them that say nothing more (``only_handoff``).
HANDOFF = re.compile(r"(?:放到|放进|放在)\s*\**T\d+\**\s*(?:「[^」]*」)?\s*里(?:做|处理|跟进)?"
                     r"|接着在\s*\**T\d+\**\s*(?:「[^」]*」)?\s*里(?:做|处理|跟进)?"
                     r"|(?:补充给|转给|交给|追加到|开了)\s*\**T\d+\**\s*(?:「[^」]*」)?(?:\s*(?:做|处理|跟进|了))?")
FILLER = re.compile(r"好的|收到|已经|这件事|这个|我|已|了|把|先|新|开|去|并|和|也|都")
LEFTOVER = 2  # Characters left after the hand-off sentences that still count as nothing more.


def only_handoff(text) -> bool:
    """Whether ``text``, a butler reply, only says where work went (``HANDOFF``): no answer of its own is left
    once those sentences, their filler words, punctuation and whitespace are out."""
    rest = HANDOFF.sub("", str(text or ""))
    rest = FILLER.sub("", re.sub(r"[\W_]+", "", rest))
    return len(rest) <= LEFTOVER


def handed_quietly(delivery) -> bool:
    """A quiet butler turn (core/placement.py Row.quiet) that has no card, ended done and only handed work on to
    topics (``routes``; its reply says nothing more, ``only_handoff``): no answer of its own goes out, its Get reaction
    on the owner's message says it (D1, 2026-11-08); the topic's thread or its conclusion follows. A reply with an
    answer of its own, or a topic waiting for the owner to press 开始 (no thread follows by itself), still goes out.
    Whether the reaction is there; when it is not, the answer goes out."""
    job, ctx, turn = delivery.job, delivery.ctx, delivery.turn
    if (getattr(job, "progress", None) or delivery.react is None or turn.status != "done"
            or getattr(turn.verified_outcome, "status", "") != "completed" or not placement.marked(ctx.state_dir, job)):
        return False
    verified = getattr(turn, "verified_delivery", None)
    if not only_handoff(getattr(verified, "text", None) or getattr(turn, "final_text", "")):
        return False
    try:
        rows = routes(ctx.state_dir, job_key(job.message_id, job.job_id))
        if not rows or any(WAITS_START in line for line in rows):
            return False
    except Exception as exc:  # noqa: BLE001 - the answer goes out
        delivery.runtime.log(f"main thread routes unreadable job=#{job.job_id}: {type(exc).__name__}")
        return False
    from covey.app.butler_reactions import GOT
    if not delivery.react(job, GOT):
        return False
    delivery.reacted = True
    delivery.runtime.log(f"main thread turn handed on, no answer sent job=#{job.job_id}")
    hook_topics(delivery)
    return True


HOOK_TITLE = 20  # Of a topic's title on the hook under the owner's message: more ends with "…".


def hook_rows(state_dir, key, *, profile=None, fetch=None, log=None) -> list[tuple[str, str, str]]:
    """(words "T453「标题」", the link to its thread, topic id) of each topic that was there the butler job
    ``key`` handed work on to (topic_send, butler_opened SEND), oldest first, once each. The link opens the topic's
    thread at the line topic_send put there (its record's ``thread_message_id``), else the thread itself
    (app/topic_links.py topic_thread_link); a fallback or none is a log line. A topic it opened or forked hangs its
    thread under the owner's message already, or has its topic card at the top level: none of those."""
    rows, seen = [], set()
    for _, record in sorted(butler_opened.by_job(state_dir, key), key=lambda pair: float(pair[1].get("at") or 0)):
        topic = str(record.get("topic_id") or "")
        if not butler_opened.answered(record) or butler_opened.kind_of(record) != butler_opened.SEND or not topic or topic in seen:
            continue
        seen.add(topic)
        title = str(_topic_title(state_dir, topic) or "")
        title = title if len(title) <= HOOK_TITLE else title[:HOOK_TITLE - 1] + "…"
        words = _name(state_dir, topic, _data(record)) + (f"「{title}」" if title else "")
        url, how = topic_links.topic_thread_link(state_dir, topic, at=[str(record.get("thread_message_id") or "")],
                                                 profile=profile, fetch=fetch)
        if log is not None and how != "thread_at" and how != "thread":
            log(f"hook link {'fell back to the chat' if url else 'missing'} job={str(key)[-10:]} topic={topic[:12]}")
        rows.append((words, url, topic))
    return rows


def hook_topics(delivery) -> None:
    """Under the owner's message of a quiet butler turn that only handed work on (``handed_quietly``: its Get
    reaction is all it shows), a small reply naming each topic that was there it went to, "→ T453「标题」 ›", each
    opening its thread (the owner, 2026-11-10 17:59: 消息上找不到去话题的入口). A quote at the chat's top level, not a
    thread under his message (提问不开串, D1); a reply quoting it goes to the topic when there is one. Never raises."""
    from covey.app.answer_cards import owner_message
    from covey.cards.answer_card import hook_card
    from covey.core.topics import route_message

    job, ctx = delivery.job, delivery.ctx
    try:
        rows = hook_rows(ctx.state_dir, job_key(job.message_id, job.job_id), profile=ctx.profile,
                         log=delivery.runtime.log)
        owner = owner_message(ctx.state_dir, job)
        built = hook_card([(words, url) for words, url, _ in rows])
        if built is None or not owner:
            return
        cards = delivery.cards
        _, message_id = cards.messenger.create_and_send_card(
            ctx.profile, job.chat_id, built, f"main-hook-{job.message_id}", voice=cards.voice,
            reply_to_message_id=owner, in_thread=False)
        if message_id and len(rows) == 1:
            route_message(ctx.state_dir, job.chat_id, message_id, rows[0][2], holder=rows[0][2])
        delivery.runtime.log(f"main thread hook {'sent' if message_id else 'not sent'} job=#{job.job_id} "
                             f"topics={len(rows)} message={message_id[-8:] if message_id else ''}")
    except Exception as exc:  # noqa: BLE001 - the Get reaction still says it went on
        delivery.runtime.log(f"main thread hook failed job=#{job.job_id}: {type(exc).__name__}")


def react_end(delivery) -> None:
    """A quiet butler turn's OnIt becomes its end (app/butler_reactions.py ending), unless ``handed_quietly`` put
    it there. Never raises."""
    from covey.app.butler_reactions import ending
    job, ctx, turn = delivery.job, delivery.ctx, delivery.turn
    try:
        if delivery.reacted or delivery.react is None or not placement.marked(ctx.state_dir, job):
            return
        routed = turn.status == "done" and bool(routes(ctx.state_dir, job_key(job.message_id, job.job_id)))
        refused = bool(getattr(getattr(turn, "outcome", None), "policy_event", None))
        delivery.reacted = True
        delivery.react(job, ending(turn.status, routed, refused))
    except Exception as exc:  # noqa: BLE001 - only the reaction is missing
        delivery.runtime.log(f"main thread end reaction failed job=#{job.job_id}: {type(exc).__name__}")


def supplements(state_dir, job) -> list[dict]:
    """The owner's messages the job took as supplements (its delivery records, merged into its request or sent to
    it), oldest first; not those it refused (they queued as the next turn) or the owner withdrew. ``job``: the job,
    or its record (job_key, job_id, topic_id)."""
    if isinstance(job, dict):
        key, job_id, topic, since = job.get("job_key") or job.get("message_id"), job.get("job_id"), job.get("topic_id"), 0
    else:
        key, job_id, topic = job.message_id, job.job_id, job.topic_id
        since = float((getattr(job, "timings", None) or {}).get("received") or 0)
    since -= CLOCK_SLACK
    found = []
    for path in (Path(state_dir) / "interaction-deliveries").glob("*.json"):
        try:
            if path.stat().st_mtime < since:
                continue
        except OSError:
            continue
        record = read_delivery(path)
        if (isinstance(record, dict) and record.get("job_key") == key
                and str(record.get("job_id")) == str(job_id) and record.get("topic_id") == topic
                and record.get("status") not in {"refused", "withdrawn"}):
            found.append(record)
    return sorted(found, key=lambda record: float(record.get("saved_at") or 0))


def dropped_lines(state_dir, job, refused) -> str:
    """The card's note of the supplements a stopped (or, ``refused``, an officially refused) turn took; "" without.
    ``job``: as ``supplements`` takes it."""
    records = supplements(state_dir, job)
    if not records:
        return ""
    how = "被拒" if refused else "停了"
    rows = [f"- {time.strftime('%H:%M', time.localtime(float(r.get('saved_at') or 0)))} · "
            + safe_text(" ".join(shown_preview(r).split()), PREVIEW) for r in records[:LISTED]]
    if len(records) > LISTED:
        rows.append(f"- 另有 {len(records) - LISTED} 条")
    return "\n".join([f"**这一轮{how}，期间你发的 {len(records)} 条补充也一起停了**，不会自动重发；要的话改一改再发：", *rows])


def show_dropped(delivery) -> bool:
    """A butler turn stopped or refused: its card lists its supplements (``dropped_lines``). Best effort."""
    job, ctx, turn = delivery.job, delivery.ctx, delivery.turn
    if not traits_of(job).lists_supplements or not getattr(job, "progress", None):
        return False
    refused = bool(getattr(getattr(turn, "outcome", None), "policy_event", None))
    if not (refused or turn.status == "cancelled"):
        return False
    try:
        text = dropped_lines(ctx.state_dir, job, refused)
        # Under the body, never folded in a slim card's 详情: the owner has to see what to send again.
        return bool(text) and delivery.cards.set_task_element(ctx, job, md(text, element_id=DROPPED), under=True)
    except Exception as exc:  # noqa: BLE001 - only the list is missing
        delivery.runtime.log(f"main thread supplements not listed job=#{job.job_id}: {type(exc).__name__}")
        return False
