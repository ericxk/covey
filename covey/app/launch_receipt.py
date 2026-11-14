"""Replace a submitted launch form with its immutable topic configuration (cards/launch_receipt.py)."""
from __future__ import annotations


from covey.app.form_cards import LAUNCH_FORM_LOCK
from covey.cards.launch_receipt import build_receipt_card
from covey.core.card_sequence import file_card, reserve
from covey.core.locks import resource_lock
from covey.core.state import read_json, write_json


def publish_receipt(app, ctx, path):
    # The directory picker uses the same lock and sequence. Never hold the
    # global workspace lock over network I/O or repeat task submission here.
    try:
        with resource_lock(LAUNCH_FORM_LOCK, path):
            record = read_json(path)
            if (record.get('status') != 'done' or not record.get('card_id')
                    or not record.get('selection') or record.get('receipt_published')):
                return
            handle = file_card(path, record, shared=False)
            with handle.lock:
                sequence = reserve(handle)
                result = app.messenger.put_card(ctx.profile, record['card_id'], build_receipt_card(record['selection']),
                                                sequence=sequence, key=f'launch-receipt-{path.stem}-{sequence}', timeout=3)
            if result is not None:
                record['receipt_published'] = True
                write_json(path, record, ensure_ascii=False)
            else:
                app.runtime.log('launch receipt pending; task already submitted')
    except Exception as exc:
        app.runtime.log(f'launch receipt unavailable: {type(exc).__name__}; task already submitted')
