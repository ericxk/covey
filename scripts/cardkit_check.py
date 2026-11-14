#!/usr/bin/env python3
"""Create-only CardKit check of card JSON: each card is registered with CardKit as the bot
(POST /open-apis/cardkit/v1/cards, as the Bridge creates a task card) and sent to no chat.

    python3 -B scripts/cardkit_check.py --profile BOT_PROFILE [--out DIR] CARD ...
    python3 -B scripts/cardkit_check.py --dry-run [--out DIR] CARD ...   # build and measure only
    python3 -B scripts/cardkit_check.py --list                           # the sample names

CARD is a name from tests/test_cards.py sample_cards() (``samples``: every one), built as the
tests build them, or a JSON file holding one card or {name: card, ...}. Each card prints one JSON
line: its name, JSON bytes, CardKit nodes (cards/elements.py element_count), whether it is within
LIMITS, ok, and the card id or the error; then "N/M created". --out DIR also writes each card as
<name>.json, the CardKit calls to cardkit.log and the lines to results.json. Exit 0 when every card
was created (--dry-run: is within LIMITS), 1 when one was not, 2 for an unknown CARD.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from covey.cards.elements import element_count  # noqa: E402

LIMITS = {"nodes": 200, "bytes": 30000}  # CardKit's bounds for one card: tagged nodes, JSON size.
CARD_KEYS = {"schema", "config", "header", "body", "elements", "i18n_elements", "card_link"}  # Top-level keys of a card.
CREATE = "/open-apis/cardkit/v1/cards"


def samples(work) -> dict[str, dict]:
    """tests/test_cards.py sample_cards(work). Importing the tests scrubs the Bridge's settings from the
    environment (tests/support); they are put back afterwards for the CardKit calls."""
    saved = dict(os.environ)
    if str(REPO / "tests") not in sys.path:
        sys.path.insert(0, str(REPO / "tests"))
    try:
        from test_cards import sample_cards
        return sample_cards(Path(work))
    finally:
        os.environ.clear()
        os.environ.update(saved)


def load(path) -> dict[str, dict]:
    """The cards in a JSON file: one card (named after the file) or {name: card}."""
    data = json.loads(Path(path).read_text())
    if isinstance(data, dict) and data and not CARD_KEYS & set(data) and all(isinstance(v, dict) for v in data.values()):
        return data
    return {Path(path).stem: data}


def measure(card) -> dict:
    size, nodes = len(json.dumps(card, ensure_ascii=False).encode()), element_count(card)
    return {"bytes": size, "nodes": nodes, "within_limits": nodes <= LIMITS["nodes"] and size <= LIMITS["bytes"]}


def create(profile, card, log) -> tuple[str, str]:
    """(card id, "") when CardKit created the card, else ("", why). Only CardKit's create call is made."""
    from covey.feishu import cardkit, transport
    from covey.feishu.messenger import card_json

    transport.set_failure(None)
    payload = cardkit.cardkit_api(profile, "POST", CREATE, card_json(card), log=log)
    card_id = cardkit.extract_card_id(payload or {})
    return card_id, "" if card_id else str(transport.last_failure() or payload or "no card id")[:500]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--profile", help="the bot's lark-cli profile (required unless --dry-run or --list)")
    parser.add_argument("--out", type=Path, help="write each card's JSON, cardkit.log and results.json here")
    parser.add_argument("--dry-run", action="store_true", help="build and measure, call nothing")
    parser.add_argument("--list", action="store_true", help="print the sample names")
    parser.add_argument("cards", nargs="*", help="sample names, samples, or JSON files")
    args = parser.parse_args(argv)
    if not (args.list or args.cards):
        parser.error("name at least one CARD (or --list)")
    if not (args.list or args.dry_run or args.profile):
        parser.error("--profile is required to create cards (or --dry-run)")
    with tempfile.TemporaryDirectory(prefix="cardkit-check-") as work:
        built = samples(work) if args.list or any(not Path(name).is_file() for name in args.cards) else {}
    if args.list:
        print("\n".join(built))
        return 0
    cards = {}
    for name in args.cards:
        if Path(name).is_file():
            cards.update(load(name))
        elif name == "samples":
            cards.update(built)
        elif name in built:
            cards[name] = built[name]
        else:
            print(f"unknown card {name!r}: not a file nor one of --list", file=sys.stderr)
            return 2
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
    calls = []

    def log(line):
        calls.append(line)
        if args.out:
            with open(args.out / "cardkit.log", "a") as f:
                f.write(line + "\n")

    results = []
    for name, card in cards.items():
        if args.out:
            (args.out / f"{name}.json").write_text(json.dumps(card, ensure_ascii=False, indent=1))
        row = {"card": name, **measure(card)}
        if args.dry_run:
            row["ok"] = row["within_limits"]
        else:
            card_id, why = create(args.profile, card, log)
            row.update(ok=bool(card_id), **({"card_id": card_id} if card_id else {"error": why}))
        results.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    if args.out:
        (args.out / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=1))
    done = sum(row["ok"] for row in results)
    print(f"{done}/{len(results)} {'within limits' if args.dry_run else 'created'}")
    return 0 if done == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
