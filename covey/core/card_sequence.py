"""One rule for the sequence of every CardKit write to an existing card.

CardKit applies a write only when its sequence is larger than that of every
earlier write to the same card, including writes whose outcome was never known.
A card can be written by several processes over its life: the bot publishes a
task card, the task runner streams into it, a later bot redraws it from the job
record. Every write therefore goes through a card handle (the card's id, the
last sequence this process used or saw for it, where that sequence is saved, and
the card's lock) and takes its sequence from ``reserve``:

- next = last + 1, saved before the request goes out and never handed out again,
  whatever the request's outcome;
- a handle opened from what is saved about a card that another process may also
  have written starts at max(saved values, current second), since an older
  release's writer saved nothing and earns at most one write a second;
- a writer that writes often (the live task card) saves a ceiling LEASE above
  what it has used and saves again only when it passes it, so whoever opens the
  card next starts above every sequence the writer used.

Sequences stay where they were always saved: a job record's card_sequence, a
browse card's sequence, a launch form's sequence (and config_sequence). They
only grow, so older releases reading them stay correct.
"""
from __future__ import annotations

import time
from pathlib import Path

from covey.core.jobs import job_path, read_job_record, upsert_job_record
from covey.core.locks import card_lock, resource_lock
from covey.core.state import write_json

LEASE = 1000


class CardHandle:
    """A card written from what is saved about it. `shared`: another process may have written it since."""

    def __init__(self, card_id, save, *saved, shared=True):
        self.card_id = card_id
        self.save_sequence = save
        # compat(d46ccbc): a writer of an older release saved no sequence and earned one write a second; retire
        # when no job record with a task card lacks card_sequence (the oldest other card records then too) and
        # no task runner before d46ccbc runs (compat_report: card-sequence-floor), and once a live task card's
        # write no longer goes out when saving its ceiling failed (app/task_card.py save_sequence): until then
        # the current second is what keeps the next writer above such a write
        self.sequence = max([0, *(int(value or 0) for value in saved)] + ([int(time.time())] if shared else []))

    @property
    def lock(self):
        return card_lock(self)


def reserve(card):
    """The sequence of one write to `card`, a CardHandle or a live task card; the caller holds card_lock(card)."""
    card.sequence += 1
    save = getattr(card, 'save_sequence', None)
    if save is not None:
        save(card.sequence)
    return card.sequence


def adopt(card, *saved):
    """Take over a live card another process may have written since it was saved."""
    card.sequence = max([int(card.sequence or 0), int(time.time()), *(int(value or 0) for value in saved)])
    return card


def raise_card_sequence(state_dir, key, value):
    """Raise job record `key`'s card_sequence to at least `value`; returns what it holds, 0 without a record.

    A missing record is never created here."""
    path = job_path(Path(state_dir), key)
    with resource_lock('record', path.absolute()):
        record = read_job_record(path)
        if record is None:
            return 0
        held = int(record.get('card_sequence') or 0)
        if held >= value:
            return held
        upsert_job_record(Path(state_dir), key, card_sequence=value)
        return value


def job_card(state_dir, key, card_id, *also):
    """The task card of job record `key`, opened from the record as it is now; hold the card's lock."""
    record = read_job_record(job_path(Path(state_dir), key)) or {}
    return CardHandle(card_id, lambda sequence: raise_card_sequence(state_dir, key, sequence),
                      record.get('card_sequence'), *also)


def file_card(path, record, *, card_field='card_id', sequence_field='sequence', shared=True, also=()):
    """A card whose sequence is saved in the JSON record at `path`: a browse card or a launch form.

    Read `record` under the card's lock. A launch form is written only by the bot, which saves each
    sequence before using it: open it with shared=False and it goes on from the saved value."""
    def save(sequence):
        record[sequence_field] = sequence
        write_json(Path(path), record, ensure_ascii=False)
    return CardHandle(record[card_field], save, record.get(sequence_field), *also, shared=shared)
