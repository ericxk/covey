"""Checks that a memory write's proof is the owner's words (WAVE12 C8): the only maker of core/memory_gate.py ``Proven``
(tests/test_structure.py MemoryWriteTest), in the app layer, where who is served is known (app/access.py).

- ``by_click``: the owner's own click (a card's, the web page's) proves itself: app/access.py ``is_owner``.
- ``prove``: a Proof of a message, a question or a quote. Readers first (W1-0): no write asks it yet, and it proves none
  (None: a write without proof); the checks of the owner's message and his quoted words, and of a question he answered
  himself, come with the package that moves the writes to core/memory_gate.py (W1-D).
"""
from __future__ import annotations

from covey.core import turn_source
from covey.core.memory_gate import Proof, Proven


def by_click(access, open_id: str, via: str) -> Proven | None:
    """A Proven of the owner's level for a click by ``open_id`` through ``via`` when it is the owner's (``access.is_owner``);
    None for anybody else's, or when it cannot be told."""
    try:
        owner = bool(open_id) and bool(access.is_owner(open_id))
    except Exception:  # noqa: BLE001 - not known to be his
        owner = False
    return Proven(Proof(via=str(via or "")), turn_source.LEVEL_OWNER) if owner else None


def prove(access, state_dir, proof: Proof, *, job_key: str = "") -> Proven | None:
    """The Proven of ``proof`` when the owner's words it names are found (in the job ``job_key``'s message, the question
    it names); None when they are not. W1-0: always None."""
    return None
