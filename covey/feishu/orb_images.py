"""The orbs' image keys (docs/ui.md 光球): each state's orb (core/orb_image.py) uploaded once as the bot of a profile,
its key kept in ``<state>/look/orbs.json`` under the profile, the state and the drawing's hash, so a redrawn orb is
uploaded again and nothing else is. A card that shows an orb asks ``orb_key``; an upload that fails gives "" and the
card goes without its orb (cards/ui.py conclusion_band), never without the card.

Called off the record locks (rule 1): the upload is a network request."""
from __future__ import annotations

import threading
from pathlib import Path

from covey.core import orb_image
from covey.core.state import ensure_private_dir, read_object, write_json
from covey.feishu.outgoing_images import upload_image

FOLDER = "look"
INDEX = "orbs.json"
_LOCK = threading.Lock()  # One upload at a time in this process; another process at worst uploads the same drawing.


def orb_key(state_dir, profile, key, *, upload=upload_image) -> str:
    """The image key of ``key``'s orb for ``profile``'s bot, uploaded the first time; "" when ``key`` has no orb or the
    upload fails."""
    if key not in orb_image.COLORS:
        return ""
    folder = Path(state_dir) / FOLDER
    name = f"{profile or 'default'}:{key}:{orb_image.digest(key)}"
    with _LOCK:
        known = read_object(folder / INDEX)
        if known.get(name):
            return str(known[name])
        ensure_private_dir(folder)
        path = folder / f"orb-{key}-{orb_image.digest(key)}.png"
        if not path.exists():
            path.write_bytes(orb_image.png(key))
        found = upload(path, profile) or ""
        if found:
            write_json(folder / INDEX, {**known, name: found})
        return found
