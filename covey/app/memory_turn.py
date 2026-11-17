"""What a turn reads of the owner's memory (COVEY_MEMORY=1, core/memory.py, docs/butler.md 记忆), for app/runs.py: ``block`` is
the one block that stands in for the principles', the knowledge entries' and the project memory's own (a new session's rules and
a resumed session's reminder carry it whole every turn), ``record`` writes the numbers of the entries a turn carried into its
job record (``memory_ids``), ``fix_rule`` whether the turn's rules tell the model it may write ``memory`` and ``memory_void`` into
its checkpoint (app/memory_collect.py reads them once the turn has ended).

Who reads what is as it was: a 代答 topic nothing; the owner's 全局 entries only on the owner's own turn (``owner_turn()``), the
workspace's on any; the project's lines in an ordinary topic of a project; the topic's own always. A line from a topic with
redaction on is only read by one with it on (a setting that cannot be read counts as off here). Nothing is written but
``memory_ids``; nothing raises into the turn."""
from __future__ import annotations

from covey.app import knowledge_turn, project_context
from covey.app.prompts import memory_block
from covey.core import knowledge, memory, principles, topic_memory
from covey.core.butler import is_butler_topic
from covey.core.jobs import job_key, upsert_job_record
from covey.core.redaction import topic_redaction
from covey.core.topics import proxy_topic

WORDS = ("以后", "每次", "别再", "不要再", "记住", "再也不要", "从现在起")  # The owner's words that start a correction.
SAVED_WORDS = ("记一下", "记住", "记下来")  # COVEY_KNOWLEDGE's own (app/knowledge_collect.py): it takes those.


def active(ctx) -> bool:
    return memory.enabled() and not proxy_topic(ctx)


def redacted(ctx, topic_id) -> bool:
    """Whether the topic has redaction on; off when it cannot be read (the lines that need it are then left out)."""
    try:
        return bool(topic_redaction(ctx.state_dir, topic_id))
    except ValueError:
        return False


def gather(ctx, job, owner_turn) -> list:
    topic_id = getattr(job, "topic_id", "") or ctx.topic_id
    owner = owner_turn()
    state = ctx.state_dir
    mine, space = principles.for_turn(state, ctx.workspace_name)
    entries = knowledge.for_turn(state, ctx.workspace_name) if knowledge.enabled() else ((), ())
    project_id, rows = project_context.memory_rows(ctx, job)
    return memory.items(principles=(mine if owner else [], space), entries=(entries[0] if owner else [], entries[1]),
                        project=rows, topic=topic_memory.live(state, topic_id), project_id=project_id,
                        redacted=redacted(ctx, topic_id))


def build(ctx, job, owner_turn) -> tuple[list, int]:
    return memory.select(gather(ctx, job, owner_turn))


def block(ctx, job, *, owner_turn) -> str:
    """The memory block of ``job``'s turn; '' with COVEY_MEMORY off, for a 代答 topic, or with nothing to say."""
    try:
        return memory_block(*build(ctx, job, owner_turn)) if active(ctx) else ""
    except Exception:  # noqa: BLE001 - the turn goes on without it
        return ""


def record(ctx, job, owner_turn, log=None) -> None:
    """Write the numbers of the entries the turn's rules carry into its job record (``memory_ids``: [{n, ref, scope, by}])."""
    try:
        if not active(ctx):
            return
        kept, _ = build(ctx, job, owner_turn)
        upsert_job_record(ctx.state_dir, job_key(job.message_id, job.job_id), memory_ids=memory.ids_of(kept))
    except Exception as exc:  # noqa: BLE001 - the record is a convenience for the correction after the turn
        if log is not None:
            log(f"memory ids not recorded: {type(exc).__name__}")


def triggered(text, *, knowledge_takes=False) -> bool:
    """Whether the owner's words of the turn hold one of ``WORDS``; ``knowledge_takes``: COVEY_KNOWLEDGE's 记住 and the like
    are its own (the entry goes there, once)."""
    text = str(text or "")
    return any(word in text for word in WORDS if not (knowledge_takes and word in SAVED_WORDS))


def fix_rule(ctx, job, owner_turn) -> bool:
    """Whether the rules of this turn carry the memory correction rule: memory on, the owner's own turn in a topic (not the
    butler's, which has its tools; not one the butler handed on), and their words hold one of ``WORDS``."""
    try:
        if not active(ctx) or not owner_turn() or getattr(job, "prompt_text", None):
            return False
        if is_butler_topic(getattr(job, "topic_id", "") or ctx.topic_id, getattr(job, "chat_id", "")):
            return False
        takes = knowledge_turn.remembers(ctx, job, owner_turn=owner_turn)
        return triggered(getattr(job, "user_text", ""), knowledge_takes=takes)
    except Exception:  # noqa: BLE001
        return False
