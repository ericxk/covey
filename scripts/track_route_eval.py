#!/usr/bin/env python3
"""How well the message router sends messages (core/track_route.py, app/track_judge.py) against a labelled corpus.

    python3 -B scripts/track_route_eval.py [--corpus FILE] [--judge new|merge|model] [--json] [--check] [--show N]
                                          [--claude-cmd CMD] [--state-dir DIR]

The corpus is JSON lines, one case per line (the default is the synthetic tests/fixtures/track_route/corpus.jsonl; real
messages stay in a local file outside the repository, same format):

    {"id": "x", "text": "...", "live": [[phase, request, started_ago_s, last_owner_ago_s], ...], "quote": "none",
     "bare": false, "enabled": true, "label": "new|merge|stop", "rule": "9", "note": ""}

``live`` lists the family's live tasks, tracks 1, 2, ... in order. ``quote`` is ``none``, ``ended``, ``own``, ``unknown``,
or ``live:N`` / ``merged:N`` for a card of track N (or a message merged into it); a track that is not in ``live`` is a
task that ended. ``label`` is what should happen; ``rule`` (optional) is the rule that should decide, which the tests
check and this script ignores.

``--judge`` says what happens to a message the rules leave to the judge (rule 9): ``new`` (the default: the fallback of
every failure, the lower bound), ``merge`` (the worst case), or ``model`` (the real judge, COVEY_TRACK_JUDGE, one call
each). The two numbers the owner watches: a message that should start a new track but was merged ("该新开却并", pass line
3%), and one that should have been merged but started a new track ("该并却新开", pass line 30%). Standard library only.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from covey.core import track_route  # noqa: E402
from covey.core.track_route import ASK_JUDGE, LEGACY, MERGE, NEW, STOP, LiveJob, Message, Quote  # noqa: E402

DEFAULT_CORPUS = ROOT / "tests" / "fixtures" / "track_route" / "corpus.jsonl"
NOW = 1_800_000_000.0
LINE_NEW_MERGED = 0.03   # 该新开却并
LINE_MERGE_NEW = 0.30    # 该并却新开


def load_corpus(path) -> list[dict]:
    cases = []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            case = json.loads(line)
            if case.get("label") not in ("new", "merge", "stop"):
                raise ValueError(f"{path}:{number}: label must be new, merge or stop")
            cases.append(case)
    return cases


def quote_of(spec: str) -> Quote:
    kind, _, number = (spec or "none").partition(":")
    track = int(number) if number else None
    if kind == "live":
        return Quote(track_route.Q_LIVE, track, f"j{track}", f"卡片 {track}")
    if kind == "merged":
        return Quote(track_route.Q_MERGED, track, f"j{track}", f"被并进任务 {track} 的原话")
    return Quote({"none": track_route.Q_NONE, "ended": track_route.Q_ENDED, "own": track_route.Q_OWN}.get(
        kind, track_route.Q_UNKNOWN), text="" if kind == "none" else "被引用的原文")


def message_of(case: dict, *, judge: bool = True) -> Message:
    live = tuple(LiveJob(i, f"j{i}", phase, request, NOW - started, NOW - owner)
                 for i, (phase, request, started, owner) in enumerate(case["live"], 1))
    return Message(case["text"], live, quote_of(case.get("quote")), case.get("enabled", True), NOW,
                   bool(case.get("bare")), judge)


def outcome(case: dict, decision, judge: str, ask) -> str:
    """"new", "merge" or "stop": what the router did with the case."""
    if decision.action in (LEGACY, NEW):
        return "new"
    if decision.action in (MERGE, STOP):
        return decision.action
    if judge == "merge":
        return "merge"
    if judge == "model":
        return "merge" if ask(case) == "merge" else "new"
    return "new"


def evaluate(cases, judge="new", ask=None) -> dict:
    """The counts and ratios for ``cases``; ``ask(case)`` is the judge's route when ``judge`` is "model"."""
    counts, rows = Counter(), []
    for case in cases:
        decision = track_route.route(message_of(case))
        got = outcome(case, decision, judge, ask)
        label = case["label"]
        counts["total"] += 1
        counts[f"label_{label}"] += 1
        counts["ask_judge"] += decision.action == ASK_JUDGE
        counts["rule_" + decision.why] += 1
        if label == "new" and got == "merge":
            counts["new_merged"] += 1
        elif label == "merge" and got == "new":
            counts["merge_new"] += 1
        elif label == "stop" and got != "stop":
            counts["stop_missed"] += 1
        elif label != "stop" and got == "stop":
            counts["stop_wrong"] += 1
        if got != label:
            rows.append((case["id"], label, got, decision.why, decision.action))

    def ratio(part, whole):
        return counts[part] / counts[whole] if counts[whole] else 0.0

    return {"counts": dict(counts), "mistakes": rows, "judge": judge,
            "new_merged": ratio("new_merged", "label_new"), "merge_new": ratio("merge_new", "label_merge"),
            "stop_missed": ratio("stop_missed", "label_stop"), "stop_wrong": ratio("stop_wrong", "total"),
            "ask_judge": ratio("ask_judge", "total")}


def model_asker(claude_cmd: str, state_dir: str):
    """``ask(case)`` that calls the real judge (app/track_judge.py) with a bare Claude backend."""
    from covey.app import track_judge
    backend = SimpleNamespace(wrap_argv=lambda argv, ctx: argv)
    ctx = SimpleNamespace(backend_cmd=claude_cmd.split(), state_dir=state_dir)

    def ask(case):
        question = next((job[1] for job in case["live"] if job[0] == "paused"), "")
        return track_judge.judge(message_of(case), question=question, backend=backend, ctx=ctx, log=lambda _: None).route

    return ask


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    parser.add_argument("--judge", choices=("new", "merge", "model"), default="new")
    parser.add_argument("--claude-cmd", default="claude", help="the Claude Code command (--judge model)")
    parser.add_argument("--state-dir", default="", help="a directory for the judge's empty working directory (--judge model)")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--check", action="store_true", help="exit 1 above the pass lines (3% and 30%)")
    parser.add_argument("--show", type=int, default=20, help="how many mistakes to list")
    args = parser.parse_args(argv)
    if args.judge == "model" and not args.state_dir:
        parser.error("--judge model needs --state-dir")
    result = evaluate(load_corpus(args.corpus), args.judge,
                      model_asker(args.claude_cmd, args.state_dir) if args.judge == "model" else None)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        c = result["counts"]
        print(f"cases {c['total']} (new {c.get('label_new', 0)}, merge {c.get('label_merge', 0)}, stop {c.get('label_stop', 0)}); "
              f"rule 9 left to the judge: {result['ask_judge']:.0%}; judge = {args.judge}")
        print(f"该新开却并 {result['new_merged']:.1%} ({c.get('new_merged', 0)}/{c.get('label_new', 0)}, line {LINE_NEW_MERGED:.0%})")
        print(f"该并却新开 {result['merge_new']:.1%} ({c.get('merge_new', 0)}/{c.get('label_merge', 0)}, line {LINE_MERGE_NEW:.0%})")
        print(f"停止漏掉 {result['stop_missed']:.1%}, 误停 {c.get('stop_wrong', 0)}")
        for row in result["mistakes"][:args.show]:
            print("  mistake: id=%s label=%s got=%s rule=%s action=%s" % row)
    failed = result["new_merged"] > LINE_NEW_MERGED or result["merge_new"] > LINE_MERGE_NEW
    return 1 if args.check and failed else 0


if __name__ == "__main__":
    sys.exit(main())
