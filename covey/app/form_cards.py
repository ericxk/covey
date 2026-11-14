"""Form cards: a card with a record of its own that every click on it reads (the launch card, 换配置, 回退,
the directory browser, the bot menu's panels in app/menu_panel.py, ...).

A card goes out once (``publish_form``) and changes only whole (``replace_form_card``): never one element
inside its form, which the client then can no longer submit. Each replacement reserves its CardKit
sequence first and saves the record with it, so the card's next write follows it.
"""
from __future__ import annotations

import hashlib
import time

from covey.core.card_sequence import file_card, reserve
from covey.core.state import write_json
from covey.core.topics import remember_topic_message

# The resource lock of a launch card's record (<state>/launch-forms/), and of the workspace manager's: the launch card,
# its model and directory pickers, its receipt, the workspace manager and the stale-form sweep take it. The name is the
# directory picker's, where it began; other processes take it by that name, so it stays.
LAUNCH_FORM_LOCK = 'directory-picker'


def save_record(path, value):
    write_json(path, value, ensure_ascii=False)


def click_token(*parts):
    """A card's token derived from the click that opened it: a click delivered twice opens one card."""
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()[:24]


def refused(prefix, reason):
    """A refusal said on the card: ``**⚠️ prefix（HH:MM）**：reason``."""
    return f"**⚠️ {prefix}（{time.strftime('%H:%M')}）**：{reason}"


def publish_form(path, record, send, *, ready='ready', topic=None):
    """Send a form's card and save its record: 'publishing' while it goes out, then ``ready`` with the card's
    ids and sequence 0, or 'delivery_failed'. ``send()`` sends the card: (card_id, message_id), the message
    id '' when it did not go out. ``topic`` (state_dir, topic_id, chat_id) learns the card's message first,
    so a reply to it finds the topic."""
    record = {**record, 'status': 'publishing'}  # Nothing on the card acts before it is registered.
    save_record(path, record)
    cid, mid = send()
    if not mid:
        save_record(path, {**record, 'status': 'delivery_failed'})
        return cid, mid
    if topic is not None:
        remember_topic_message(*topic, mid)
    save_record(path, {**record, 'status': ready, 'card_id': cid, 'message_id': mid, 'sequence': 0})
    return cid, mid


def replace_form_card(ctx, path, record, candidate, card, messenger, kind, *, card_field='card_id',
                      sequence_field='sequence'):
    """Replace a launch form's whole card and store candidate as its record.

    A form published as two cards keeps its other card in config_card_id and config_sequence."""
    handle = file_card(path, record, card_field=card_field, sequence_field=sequence_field, shared=False)
    with handle.lock:
        # Reserved before I/O; failed/uncertain calls never reuse a CardKit sequence.
        sequence = reserve(handle)
        started = time.monotonic()
        result = messenger.put_card(ctx.profile, record[card_field], card, sequence=sequence,
                                    key=f'{kind}-{path.stem}-{sequence}', timeout=3)
    messenger.log(f'{kind} card update ok={result is not None} elapsed_ms={(time.monotonic() - started) * 1000:.0f}')
    if result is None:
        return False
    candidate[sequence_field] = sequence
    save_record(path, candidate)
    return True


def settle(ctx, path, record, candidate, card, messenger, kind):
    """``replace_form_card``, with ``candidate`` saved also when CardKit did not confirm, and with the sequence
    that write reserved (``record`` holds it): what it records has happened regardless, the next redraw shows
    it and takes the next sequence. Whether the card changed."""
    if replace_form_card(ctx, path, record, candidate, card, messenger, kind):
        return True
    save_record(path, {**candidate, 'sequence': record['sequence']})
    return False
