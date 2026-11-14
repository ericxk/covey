"""Product defaults, separate from provider-native default metadata."""

from covey.core.extensions import work_modes

RECOMMENDED = "top_minus_one"
# `--effort ultracode` is xhigh plus standing workflow orchestration; Claude Code (2.1.310) offers it on every
# model that supports xhigh while dynamic workflows are enabled, but the initialization response does not list
# it. So the Bridge offers it exactly where ULTRACODE_RUNS_AS is offered (backends/claude/models.py
# levels_with_ultracode).
ULTRACODE = "ultracode"
ULTRACODE_RUNS_AS = "xhigh"
EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra", ULTRACODE)
# Not deeper budgets: each Harness's own orchestration on xhigh. Codex's ultra sends
# xhigh and turns on proactive sub-agent delegation (codex-cli 0.164.0); Claude Code's
# ultracode is xhigh with standing workflow orchestration (2.1.310). Checked 2026-10-27.
ORCHESTRATION_MODES = frozenset({"ultra", ULTRACODE})


def recommended_effort(levels):
    """Resolve the next-to-highest budget, never silently select high. An orchestration
    mode on top does not count, so a model offering one recommends two tiers below it."""
    available = set(levels)
    if available - set(EFFORT_ORDER):
        raise ValueError("该模型包含未知推理档位，请手动选择；未自动降低预算")
    ordered = [value for value in EFFORT_ORDER if value in available and value not in ORCHESTRATION_MODES]
    if len(ordered) < 2 or ordered[-2] not in {"xhigh", "max"}:
        raise ValueError("该模型未提供符合要求的次高档（至少 xhigh）；请选择支持该档位的模型，或手动指定预算")
    return ordered[-2]


# The work modes a Harness offers (an extension's manifest, core/extensions.py: mode id -> (label, hint)) and the
# default one, listed first. Bridge records keep "" for the default, so a topic, job or runtime payload from
# before modes reads as the default, and a default one is written exactly as before them.
WORK_MODES, DEFAULT_WORK_MODE = work_modes()


def stored_work_mode(mode):
    """What a topic, job or launch default records for a chosen mode: "" for the default."""
    return "" if mode == DEFAULT_WORK_MODE else mode


def work_mode_tag(mode):
    """A mode as receipts and task cards append it ("Adaptive"); "" for the default, so those look as before."""
    return "" if not mode or mode == DEFAULT_WORK_MODE else WORK_MODES.get(mode, (mode,))[0]
