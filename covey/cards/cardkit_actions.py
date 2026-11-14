"""What a CardKit write says, as plain JSON: the actions of a ``batch_update`` and the bodies of the writes to one
part of a card. An element goes in a request as JSON text, Chinese unescaped. Nothing here sends anything: the
sequence and key are added where the write is made (feishu/messenger.py write_card)."""

from __future__ import annotations

import json


def dumps(value):
    return json.dumps(value, ensure_ascii=False)


def update(element):
    """The batch action replacing ``element`` by its element_id."""
    return {"action": "update_element", "params": {"element_id": element["element_id"], "element": dumps(element)}}


def insert_body(elements, target, before=False):
    """What adds ``elements`` after ``target`` (``before``: before it): a ``POST elements`` body, an add's params."""
    return {"type": "insert_before" if before else "insert_after", "target_element_id": target,
            "elements": dumps(elements)}


def insert(elements, target, before=False, *, as_list=False):
    """The batch action adding ``elements``. ``as_list``: as a list, not JSON text (Cards.lead_first's, which CardKit
    has taken that way since it was written)."""
    params = insert_body(elements, target, before)
    if as_list:
        params["elements"] = elements
    return {"action": "add_elements", "params": params}


def delete(*element_ids):
    """The batch action removing ``element_ids``."""
    return {"action": "delete_elements", "params": {"element_ids": list(element_ids)}}


def batch_body(actions):
    """A ``POST batch_update`` body: ``actions`` applied together; a card missing an element refuses them all."""
    return {"actions": dumps(actions)}


def element_body(element):
    """A ``PUT elements/<id>`` body: ``element`` in place of the one of its ID."""
    return {"element": dumps(element)}


def content_body(content):
    """A ``PATCH elements/<id>`` body: only the element's text."""
    return {"partial_element": dumps({"content": content})}


def settings_body(config):
    """A ``PATCH settings`` body: ``config`` over the card's."""
    return {"settings": dumps({"config": config})}
