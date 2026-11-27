#!/usr/bin/env python3
"""样张 before a card's look changes (docs/ui.md 12 改外观先选样张): the files a checkpoint question's options point at
with ``sample``, made and checked the way the Bridge will draw them (covey/core/ui_samples.py, cards/question_cards.py).

    python3 -B scripts/ui_samples.py golden KEY --out /absolute/A.json       # a card of tests/golden/cards.json: 现在的样子
    python3 -B scripts/ui_samples.py golden --list                            # the keys
    python3 -B scripts/ui_samples.py check /absolute/A.json /absolute/B.json [--labels "A 照旧（推荐）,B 改"] --out /absolute/dir
    python3 -B scripts/cardkit_check.py --profile BOT_PROFILE /absolute/dir/question-card.json   # CardKit, create only

``golden`` writes one card of the golden file as a sample (``--avatars STATE --profile P``: the speaker pictures' fake image
keys replaced by the ones this deployment uploaded, core/avatars.py, so the sample shows them). ``check`` reads each file as
the Bridge reads it when the question is registered, says for each how large it is and whether it is drawn, and writes the
question card the owner will get to ``--out``/question-card.json (``--html``: also question-card.html, a sketch drawn by
scripts/card_sketches.py's renderer). Exit 1 when a sample is not drawn. Nothing here sends anything.
"""
from __future__ import annotations

import argparse
import json
import lzma
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

GOLDEN = REPO / "tests" / "golden" / "cards.json.xz"
FAKE_KEYS = {"img_v3_fake_bu": "butler", "img_v3_fake_le": "G-1"}  # tests' avatar keys: whose picture each stands for.


def golden_cards() -> dict:
    return json.loads(lzma.open(GOLDEN).read())


def real_keys(card, state, profile):
    """``card`` with each fake speaker picture key (FAKE_KEYS, by its start) replaced by this deployment's (core/avatars.py
    cached_key); one it never uploaded stays fake, and the Bridge draws a grey mark for it."""
    from covey.core.avatars import cached_key
    text = json.dumps(card, ensure_ascii=False)
    for start, name in FAKE_KEYS.items():
        key = cached_key(state, profile, name)
        if key:
            import re
            text = re.sub(re.escape(start) + r'[^"]*', key, text)
    return json.loads(text)


def golden(args) -> int:
    cards = golden_cards()
    if args.list:
        print("\n".join(key for key, value in cards.items() if isinstance(value, dict)))
        return 0
    if not args.key or not args.out:
        print("golden: KEY and --out are needed (or --list)", file=sys.stderr)
        return 2
    if args.key not in cards or not isinstance(cards[args.key], dict):
        print(f"no card {args.key!r} in {GOLDEN.relative_to(REPO)} (--list)", file=sys.stderr)
        return 2
    card = cards[args.key]
    if args.avatars:
        card = real_keys(card, Path(args.avatars), args.profile or "")
    Path(args.out).write_text(json.dumps(card, ensure_ascii=False, indent=1), encoding="utf-8")
    print(args.out)
    return 0


def question(files, labels) -> list:
    """The checkpoint question ``check`` registers: one single-choice question, an option a file."""
    names = [label.strip() for label in labels.split(",")] if labels else []
    options = [{"label": names[i] if i < len(names) and names[i] else chr(ord("A") + i), "sample": str(Path(path).resolve())}
               for i, path in enumerate(files)]
    return [{"id": "look", "header": "样张", "question": "用哪一版？", "options": options}]


def check(args) -> int:
    from covey.cards.question_cards import CARD_BYTES, CARD_NODES, SAMPLE_LEFT_OUT, question_fit, sample_blocks
    from covey.core import ui_samples

    asked = ui_samples.carry(question(args.files, args.labels))
    record = {"token": "0" * 16, "status": "pending", "topic_id": "0" * 64, "chat_id": "oc_sample", "job_id": 1,
              "blocking": True, "questions": asked, "created_at": 0}
    fit = question_fit(record, "T1", "样张")
    card = fit.card
    drawn = [block for block, _ in sample_blocks(record)]
    # The card leaves out its last samples while it is too large (cards/question_cards.py fitted), each saying so.
    left_out = set(fit.left_out)
    bad = 0
    for i, (option, path) in enumerate(zip(asked[0]["options"], args.files)):
        kept = ui_samples.kept(option)
        why = kept.get("note") or (SAMPLE_LEFT_OUT if i in left_out else "")
        size = f"{ui_samples.size(kept['elements']) / 1000:.1f} KB、{ui_samples.nodes(kept['elements'])} 个元素" \
            if kept.get("elements") else "—"
        print(f"{option['label']}：{size}" + (f"；{why}" if why else "；画上了") + f"  ({path})")
        bad += bool(why)
    print(f"整张问题卡：{ui_samples.size(card) / 1000:.1f} KB、{ui_samples.nodes(card)} 个元素"
          f"（最多 {CARD_BYTES / 1000:.0f} KB、{CARD_NODES} 个），{len(drawn)} 版样张")
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "question-card.json").write_text(json.dumps(card, ensure_ascii=False, indent=1), encoding="utf-8")
        print(out / "question-card.json")
        if args.html:
            sys.path.insert(0, str(REPO / "scripts"))
            try:
                from card_sketches import Page
                (out / "question-card.html").write_text(Page().page([("card", card)]), encoding="utf-8")
                print(out / "question-card.html")
            except Exception as exc:  # noqa: BLE001 - the renderer stops at an element it does not know
                print(f"示意图没画：{type(exc).__name__}: {exc}", file=sys.stderr)
    return 1 if bad else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("golden", help="write a card of the golden file as a sample")
    one.add_argument("key", nargs="?")
    one.add_argument("--out")
    one.add_argument("--list", action="store_true")
    one.add_argument("--avatars", help="a state directory: its uploaded avatar keys instead of the fake ones")
    one.add_argument("--profile", help="the bot's lark-cli profile the keys were uploaded with (with --avatars)")
    many = sub.add_parser("check", help="read the samples as the Bridge does and draw the question card")
    many.add_argument("files", nargs="+")
    many.add_argument("--labels", default="", help="the options' labels, comma separated (A, B, C … without)")
    many.add_argument("--out", help="where question-card.json (and question-card.html) go")
    many.add_argument("--html", action="store_true", help="also a sketch of the card (scripts/card_sketches.py)")
    args = parser.parse_args(argv)
    return golden(args) if args.command == "golden" else check(args)


if __name__ == "__main__":
    sys.exit(main())
