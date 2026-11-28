"""Checks that a memory write's proof is the owner's words (WAVE12 C8): the only maker of core/memory_gate.py ``Proven``
(tests/test_structure.py MemoryWriteTest), in the app layer, where who is served is known (app/access.py).

- ``by_click``: the owner's own click (a card's, the web page's) proves itself: app/access.py ``is_owner``.
- ``prove``: a Proof of a quote in a turn of his (T908 M3a: 记忆自己长, a checkpoint's memory, the butler's memory_save). The
  job is his own words (core/turn_source.py ``level`` owner, of a source whose words are his, his turn by app/access.py
  ``owner_turn``) and the quote a run of them (app/memory_turn.py ``owner_words``: of an answer's turn only what he
  answered, never the question's words), whitespace aside. Anything else (no job, a quote that is not his) proves nothing:
  the write is then nobody's word but the writer's, never 你说的.
"""
from __future__ import annotations

from covey.core import turn_source
from covey.core.jobs import job_path, read_job_record
from covey.core.memory_gate import Proof, Proven


def by_click(access, open_id: str, via: str) -> Proven | None:
    """A Proven of the owner's level for a click by ``open_id`` through ``via`` when it is the owner's (``access.is_owner``);
    None for anybody else's, or when it cannot be told."""
    try:
        owner = bool(open_id) and bool(access.is_owner(open_id))
    except Exception:  # noqa: BLE001 - not known to be his
        owner = False
    return Proven(Proof(via=str(via or "")), turn_source.LEVEL_OWNER) if owner else None


def _squash(text) -> str:
    return "".join(str(text or "").split())


def said_in(quote, said) -> bool:
    """Whether ``quote`` (not empty) is a run of ``said``, whitespace aside."""
    quote = _squash(quote)
    return bool(quote) and quote in _squash(said)


def owner_words(access, state_dir, record) -> str:
    """The owner's own words of the turn of job ``record`` when they are his (the module's docstring); '' when they are not,
    or cannot be told."""
    from covey.app.memory_turn import owner_words as words

    if not isinstance(record, dict) or turn_source.level(record) != turn_source.LEVEL_OWNER:
        return ""
    if not turn_source.behavior(record).owner_words:
        return ""
    try:
        if not access.owner_turn(str(record.get("chat_id") or ""), str(record.get("sender_id") or "")):
            return ""
    except Exception:  # noqa: BLE001 - not known to be his
        return ""
    return words(record, state_dir)


def prove(access, state_dir, proof: Proof, *, job_key: str = "") -> Proven | None:
    """The Proven of ``proof`` when the owner's words it quotes are found in the job ``job_key``'s turn (the module's
    docstring): of his level, the job's message id its ``message_id`` (one ``proof`` names must be that one), the question the
    turn answered its ``question``; None when
    there is no such job, it is not his words, or ``proof.quote`` is not a run of them."""
    record = read_job_record(job_path(state_dir, job_key)) if job_key and "/" not in job_key else None
    if record is None or not proof.quote:
        return None
    message = str(record.get("message_id") or "")
    if proof.message_id and proof.message_id != message:
        return None
    said = owner_words(access, state_dir, record)
    if not said_in(proof.quote, said):
        return None
    question = turn_source.origin(record).get("question", "")  # The question the turn answered, as its source says.
    return Proven(Proof(message_id=message, question=question if proof.question in ("", question) else "", quote=proof.quote,
                        via=proof.via), turn_source.LEVEL_OWNER, said=proof.quote)
