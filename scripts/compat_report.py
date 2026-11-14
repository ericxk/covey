#!/usr/bin/env python3
"""Whether the compatibility code marked ``# compat(<commit>): …; retire when …`` can go, from live data.

    python3 -B scripts/compat_report.py --state "$STATE_DIR" [--harness-config FILE] [--service-root DIR]
        [--repo DIR] [--json]

Each marked place ends its comment with ``(compat_report: <check>)``; CHECKS below has one entry per
check, with the commit that brought the code and the files it is in (tests/test_compat_report.py keeps
the two in step). A check counts what still needs the code: records in the state directory (jobs/,
topics/, launch-forms/, switch-forms/, job-diffs/, proxy/panels/), the task runners alive now (runs/), the Harness config (``--harness-config``, by
default <data home>/harnesses.json next to the state directory; when it cannot be read, its checks say
不知道) and the service's manifest under ``--service-root`` (default: the Manager's root, as
bin/covey_paths.py service-root prints; when it cannot be read, its check says 不知道). A task runner's release is placed against a commit with git in
``--repo`` (default: the checkout this script is in); without git that check says 不知道 and the code
stays.

REMOVED lists code already taken out (with the batch that did it): the condition it was removed on
must keep holding until the task runners it served are gone, so a count there makes the exit status 1.
Read-only: no write, no lock, no network. Run it before a deploy that removes one, or at any time.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from covey.core.process import service_key  # noqa: E402
from covey.core.providers import RETIRED_PREFIXES  # noqa: E402
from covey.core.roster import NAMES  # noqa: E402
from covey.core.run_dir import HANDOFF, RUNNER, RUNS, runner_alive  # noqa: E402


@dataclass(frozen=True)
class Check:
    name: str
    commit: str  # The commit that brought the code, as its # compat(...) comment says.
    files: tuple[str, ...]  # Where it is, relative to covey/.
    count: Callable  # Facts -> (how many still need it, None when unknown; what they are).


class Facts:
    """What the checks read, each once."""

    def __init__(self, state: Path, repo: Path, harness_config: Path | None = None, service_root: Path | None = None):
        self.state, self.repo = state, repo
        self.harness_config = harness_config or state.parent / "harnesses.json"
        self.service_root = service_root
        self.jobs = list(_records(state / "jobs"))
        self.runners = []  # (run directory, its runner.json), alive now.
        runs = state / RUNS
        for run in sorted(runs.iterdir()) if runs.is_dir() else []:
            if run.is_dir() and not run.is_symlink() and runner_alive(run):
                self.runners.append((run, _read(run / RUNNER) or {}))
        self._contains = {}

    def contains(self, release, commit):
        """Whether the code of ``release`` (a runner.json's release path) has ``commit``; None: unknown."""
        name = Path(str(release or "")).name
        if (name, commit) not in self._contains:
            found = None
            if len(name) == 40 and all(c in "0123456789abcdef" for c in name):
                try:
                    found = subprocess.run(["git", "-C", str(self.repo), "merge-base", "--is-ancestor", commit, name],
                                           capture_output=True, timeout=30).returncode
                    found = {0: True, 1: False}.get(found)
                except (OSError, subprocess.SubprocessError):
                    found = None
            self._contains[(name, commit)] = found
        return self._contains[(name, commit)]

    def runners_before(self, commit):
        """Live task runners whose release does not have ``commit`` (None when one is unknown)."""
        seen = [(run, self.contains(runner.get("release"), commit)) for run, runner in self.runners]
        if any(has is None for _, has in seen):
            return None, "不知道这些任务进程的 release：" + " ".join(run.name for run, has in seen if has is None)
        old = [run.name for run, has in seen if not has]
        return len(old), ("任务进程 " + " ".join(old)) if old else ""


def _read(path):
    try:
        value = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _records(folder):
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        if not path.name.startswith(".") and (record := _read(path)) is not None:
            yield path, record


def retired_topics(facts):
    names = {}
    for path in (facts.state / "topics").glob("*/topic.json"):
        name = (_read(path) or {}).get("harness_id")
        if name and name not in NAMES:
            names[name] = names.get(name, 0) + 1
    return sum(names.values()), "、".join(f"{name} {n}" for name, n in sorted(names.items()))


def retired_provider_choices(facts):
    """Topics on a removed provider's model (core/providers.py RETIRED_PREFIXES), and launch defaults and recent
    model choices that name one: Claude Code refuses the topics, the menus skip the choices."""
    from covey.app.launch import CHAT_DEFAULTS
    from covey.app.model_usage import USAGE

    def retired(key):  # A model id, or a route key "harness:model".
        key = str(key or "")
        return any(part.startswith(tuple(RETIRED_PREFIXES)) for part in (key, key.partition(":")[2]))

    topics = sum(1 for path in (facts.state / "topics").glob("*/topic.json") if retired((_read(path) or {}).get("model")))
    defaults = sum(1 for _, record in _records(facts.state / CHAT_DEFAULTS) if retired(record.get("model_choice")))
    events = (_read(facts.state / USAGE) or {}).get("events")
    choices = sum(1 for event in events if isinstance(event, dict) and retired(event.get("model"))) \
        if isinstance(events, list) else 0
    detail = "、".join(f"{label} {n}" for label, n in (("话题", topics), ("启动默认", defaults), ("最近选择", choices)) if n)
    return topics + defaults + choices, detail


def _harness_config(facts):
    """The Harness config's text; None when it cannot be read (then whether it needs the code is unknown)."""
    try:
        return facts.harness_config.read_text()
    except OSError:
        return None


# Claude's options kept only so an older Harness config parses: what they do went (backends/claude/bridge.py);
# a trailing "-" names a family by its prefix.
REMOVED_OPTIONS = ("--claude-transport", "--traex-", "--claude-profiles")


def removed_options(facts):
    """Those options still passed in the Harness config, as an argv item or as --option=value."""
    if (text := _harness_config(facts)) is None:
        return None, f"读不到 {facts.harness_config}"
    counts = {option: text.count('"' + option) for option in REMOVED_OPTIONS}
    detail = "、".join(f"{option}{'*' if option.endswith('-') else ''} {n}" for option, n in counts.items() if n)
    return sum(counts.values()), f"{facts.harness_config.name} 里 {detail}" if detail else ""


def codex_option(option):
    """The check of a removed Codex ``option`` still passed where Codex's options are parsed: the default
    Harness's are the service's command line (its manifest), which every task runner's handoff argv copies; the
    Harness config's "codex" entry is never read. Unknown when the manifest cannot be read."""
    def passes(argv):
        return isinstance(argv, list) and any(str(arg).partition("=")[0] == option for arg in argv)

    def check(facts):
        root = facts.service_root
        if root is None:
            from covey.core.paths import service_root
            root = service_root()
        manifest = _read(root / "services" / (service_key(facts.state) + ".json"))
        if manifest is None:
            return None, f"读不到 {root} 里这个状态目录的服务清单"
        runners = [run.name for run, _ in facts.runners if passes((_read(run / HANDOFF) or {}).get("argv"))]
        service = passes(manifest.get("command"))
        detail = "、".join(part for part in ("服务命令行" if service else "",
                                            "任务进程 " + " ".join(runners) if runners else "") if part)
        return int(service) + len(runners), detail

    return check


codex_transport = codex_option("--codex-transport")


def doubao_jobs(facts):
    """Job records of the removed 豆包 Harness, which the Claude Harness's card buttons still find."""
    count = sum(1 for _, record in facts.jobs if record.get("backend") == "doubao")
    return count, ""


def retired_launch_forms(facts):
    from covey.app.launch import retired_form
    found = [path.name for path, record in _records(facts.state / "launch-forms") if retired_form(record)]
    return len(found), ""


def embedded_traces(facts):
    from covey.core.trace_store import trace_path
    newest, count = "", 0
    for _, record in facts.jobs:
        if not (record.get("trace_snapshot") or record.get("tool_steps")) or not record.get("key"):
            continue
        path = trace_path(facts.state, str(record["key"]))
        if path.exists() or path.with_name(path.name + ".gz").exists():
            continue
        count += 1
        newest = max(newest, str(record.get("updated_at") or record.get("created_at") or ""))
    return count, f"最新一条 {newest[:10]}" if count else ""


def unsaved_card_sequence(facts):
    jobs = [record for _, record in facts.jobs if record.get("card_id") and not record.get("card_sequence")]
    runners, detail = facts.runners_before("d46ccbc")
    if runners is None:
        return None, detail
    newest = max((str(r.get("updated_at") or "") for r in jobs), default="")
    return len(jobs) + runners, "；".join(part for part in (
        f"{len(jobs)} 条有任务卡、没有 card_sequence 的任务记录（最新一条 {newest[:10]}）" if jobs else "", detail) if part)


def local_state(facts):
    if (text := _harness_config(facts)) is None:
        return None, f"读不到 {facts.harness_config}"
    count = text.count("/.local-state/")
    return count, f"{facts.harness_config.name} 里 {count} 处路径在 .local-state 下" if count else ""


def panel_lookups(facts):
    found = [path.name for path, record in _records(facts.state / "proxy" / "panels") if path.name != "latest.json"
             and isinstance((record.get("view") or {}).get("found"), dict) and not record["view"].get("lookup")]
    return len(found), " ".join(found)


def paged_cards(facts):
    """Job records with a task card sent before dd3ee63 went live (2026-11-04), whose process panel has page
    controls; one without created_at is older."""
    found = [str(record.get("created_at") or "") for _, record in facts.jobs
             if record.get("card_id") and str(record.get("created_at") or "") < "2026-11-05"]
    return len(found), f"最新一条 {max(found)[:10]}" if any(found) else ""


def runners_before(commit):
    return lambda facts: facts.runners_before(commit)


def removed_harness_runners(facts):
    """Live task runners whose handoff names a Harness out of the roster (or cannot be read)."""
    old = [run.name for run, _ in facts.runners if (_read(run / HANDOFF) or {}).get("harness") not in NAMES]
    return len(old), " ".join(old)


def auto_retry_kinds(facts):
    """Scheduled or submitting automatic retries without a kind: the ones app/auto_retry.py kind_of still reads. A
    policy stop's auto_retry_state "cancelled" has no kind and is never read for one."""
    return sum(1 for _, record in facts.jobs if record.get("auto_retry_state") in ("pending", "submitting")
               and not record.get("auto_retry_kind")), ""


def fat_diff_indexes(facts):
    """job-diffs/*/diff.json files that still repeat their diff text (core/maintenance_rules.py _fat_diff_index)."""
    count = 0
    for path in sorted((facts.state / "job-diffs").glob("*/diff.json")) if (facts.state / "job-diffs").is_dir() else []:
        try:
            with path.open("rb") as handle:
                count += b'"diff_text"' in handle.read(64 * 1024)
        except OSError:
            continue
    return count, ""


def switch_form_fields(facts):
    """换配置 records from before its workspace and 敏感词脱敏 selects."""
    found = [path.name for path, record in _records(facts.state / "switch-forms")
             if "selected_workspace" not in record or "selected_redaction" not in record]
    return len(found), " ".join(found)


def lock_recheck(facts):
    """Live task runners whose release does not notice a lock file maintenance removed (core.locks._same_file)."""
    old = []
    for run, runner in facts.runners:
        try:
            if "_same_file(" not in (Path(runner["release"]) / "covey" / "core" / "locks.py").read_text():
                old.append(run.name)
        except (OSError, KeyError, TypeError):
            old.append(run.name)
    return len(old), " ".join(old)


CHECKS = (
    Check("retired-harness", "d3aaeb6", ("app/topics.py",), retired_topics),
    Check("retired-provider", "826a0d9", ("core/providers.py",), retired_provider_choices),
    Check("removed-options", "ab0ce25", ("backends/claude/bridge.py",), removed_options),
    Check("codex-transport", "ab0ce25", ("backends/codex/bridge.py",), codex_transport),
    Check("codex-magpie-url", "58800eb", ("backends/codex/bridge.py",), codex_option("--codex-magpie-url")),
    Check("doubao-jobs", "5535387", ("app/harnesses.py",), doubao_jobs),
    Check("retired-launch-form", "3bfb9be", ("app/launch.py",), retired_launch_forms),
    Check("embedded-trace", "ff57531", ("core/migrate.py",), embedded_traces),
    Check("card-sequence-floor", "d46ccbc", ("core/card_sequence.py",), unsaved_card_sequence),
    Check("local-state", "43e99b4", ("core/paths.py",), local_state),
    Check("panel-lookup", "73dd0f3", ("cards/proxy_panel.py",), panel_lookups),
    Check("trace-pages", "dd3ee63", ("app/interaction.py",), paged_cards),
    Check("steer-refused", "b20cfed", ("app/runner_ipc.py",), runners_before("b20cfed")),
    Check("removed-harness-runners", "ff57531", ("app/detached.py",), removed_harness_runners),
    Check("policy-event-runners", "fb0b267", ("core/result.py",), runners_before("fb0b267")),
    Check("auto-retry-kind", "84ad62a", ("app/auto_retry.py",), auto_retry_kinds),
    Check("fat-diff-index", "30c3d17", ("core/maintenance_rules.py",), fat_diff_indexes),
    Check("switch-form-fields", "ecb0458", ("app/harness_switch.py", "cards/switch_form.py"), switch_form_fields),
)
# Taken out; (check, the batch that did). The count must stay 0 while a task runner it served could live.
REMOVED = (
    (Check("lock-recheck", "5ad7924", ("core/maintenance.py",), lock_recheck), "repo review batch 5a"),
)


def report(state: Path, repo: Path = REPO, harness_config: Path | None = None,
           service_root: Path | None = None) -> list[dict]:
    facts = Facts(state, repo, harness_config, service_root)
    rows = []
    for check, removed in [(c, "") for c in CHECKS] + list(REMOVED):
        count, detail = check.count(facts)
        if removed:
            verdict = "已删" if count == 0 else "已删但还有数据要它"
        else:
            verdict = "不知道" if count is None else "可删" if count == 0 else "还要留"
        rows.append({"check": check.name, "commit": check.commit, "files": list(check.files), "count": count,
                     "detail": detail, "verdict": verdict, "removed": removed})
    return rows


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="compat_report.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("--state", type=Path, required=True, help="the Bridge's state directory")
    parser.add_argument("--repo", type=Path, default=REPO, help="a git checkout that has the live releases' commits")
    parser.add_argument("--harness-config", type=Path,
                        help="the service's --harness-config (default: harnesses.json next to the state directory)")
    parser.add_argument("--service-root", type=Path,
                        help="the Manager's root with the service's manifest (default: covey_paths.py service-root)")
    parser.add_argument("--json", action="store_true", help="one JSON list instead of lines")
    args = parser.parse_args(argv)
    rows = report(args.state.absolute(), args.repo, args.harness_config and args.harness_config.absolute(),
                  args.service_root and args.service_root.absolute())
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
    else:
        for row in rows:
            count = "?" if row["count"] is None else row["count"]
            print(f"{row['verdict']}\t{row['check']}\t{' '.join(row['files'])} ({row['commit']})\t{count}"
                  + (f"\t{row['detail']}" if row["detail"] else "") + (f"\t{row['removed']}" if row["removed"] else ""))
    return 1 if any(row["removed"] and row["count"] for row in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
