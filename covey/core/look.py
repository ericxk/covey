"""How a state looks on all three surfaces (docs/ui.md 夜航): the chat's cards, the bot menu's panels and the web page.

One table, ``STATES``: a state's word, its group, its Feishu colours, its colour on the dark web page and its icon. The
cards read it through cards/ui.py, the web page through ``export`` (scripts/export_look.py writes it as look.json) and
the ``tone`` each web item carries (data contract v5). The words and colours of the five states the 看板 shows are its
own (cards/board.py LOOKS), so that it can move here without a change (tests/test_look.py checks they agree).

- ``tone``: a record's status (and whether its result was seen, whether it may be stuck, who stopped it) as a key of
  STATES. The owner, 2026-11-18 23:58: a failed, interrupted, timed-out or refused turn is 停住了, the reason on its
  second line; one he stopped himself is 已停止, background.
- ``owner_stopped``: whether a cancelled record was stopped by the owner, from ``cancel_requested_by``
  (core/jobs.py STOP_SOURCES); one cancelled while queued is his. An older running one without it is not known to be.
- ``pressing``: the most pressing state there is (``PRESSING``): what a summary leads with.
- ``BAND``, ``band_colors``: the conclusion band's background, a vertical gradient in steps too fine to see (the owner,
  2026-11-19 08:10, after four sample cards: no orb, no glow around the edge, no visible stripes). A Feishu container
  takes one solid colour, so the band is rows, each a step lighter than the one above, inside one thin line. The
  colours are the card's own (``config.style.color``), each with a light and a dark mode value.
- ``RETIRED``: words the surfaces said before for these states, to be replaced as each surface moves.

Standard library only, importing nothing of the Bridge's."""
from __future__ import annotations

from typing import NamedTuple

# What a state asks of the owner: ACT 要你处理, TELL 告诉你一声, BACK 背景.
ACT, TELL, BACK = "act", "tell", "back"


class Look(NamedTuple):
    word: str  # The word every surface says.
    group: str  # ACT, TELL or BACK.
    color: str  # Feishu's colour name: text_tag, <font> and standard_icon.
    tint: str  # Feishu's background_style of a block in this state.
    web: str  # Its colour on the web page's dark background.
    icon: str  # Feishu's standard_icon token ("" for none).


STATES = {
    "waiting": Look("等你", ACT, "orange", "orange-50", "#F5A524", "time_outlined"),
    "stopped": Look("停住了", ACT, "red", "red-50", "#F2555A", "warning_outlined"),
    "stuck": Look("可能卡住", ACT, "orange", "orange-50", "#F5A524", "warning_outlined"),
    "result": Look("有结果", TELL, "green", "green-50", "#3DD68C", "yes_outlined"),
    "running": Look("在做", BACK, "blue", "blue-50", "#4C9AFF", "loading_outlined"),
    "queued": Look("排队", BACK, "neutral", "grey-50", "#5B6270", ""),
    "done": Look("做完", BACK, "neutral", "grey-50", "#5B6270", "yes_outlined"),
    "cancelled": Look("已停止", BACK, "neutral", "grey-50", "#5B6270", ""),
    "idle": Look("空闲", BACK, "neutral", "grey-50", "#5B6270", ""),
}
# A heading of a group of blocks where it differs from the state's word.
GROUP_WORDS = {"result": "有结果没看"}
# Objects, not states.
PROJECT = Look("专项", BACK, "violet", "violet-50", "#A78BFA", "folder_outlined")
# The most pressing state first: what a summary (the 看板's conclusion, the web page's first screen) leads with.
PRESSING = ("waiting", "stopped", "stuck", "result", "running")

# The brand: the visual centre and what can be clicked. Cards in Feishu's colour names (light and dark mode are
# Feishu's), the web page in its own colours.
BRAND = {"color": "indigo", "web": "#7C83FF"}
# The conclusion band: rows from ``top`` alpha down by ``step`` (never under ``floor``) of ``rgb`` in light mode, ``dark``
# times as strong of ``dark_rgb`` in dark mode, inside a ``line`` (light, dark) alpha border. A step of 1% shows no
# stripe; 3% did (the third sample card).
BAND = {"rgb": (94, 106, 210), "dark_rgb": (124, 131, 255), "top": 0.085, "step": 0.01, "floor": 0.035, "dark": 2.0,
        "line": (0.20, 0.38), "rows": 6}
WEB = {"surface": "#08090C", "panel": "#0F1116", "line": "#1F2229", "ink": "#E6E8EB", "muted": "#8A8F98"}

# core/jobs.py STOP_SOURCES that are the owner's own: his card's 停止, his stop word to the butler, his stop on
# the web page, his stop of a project on its card, his recall of the task's message. The lead's, the guard's and 代答's
# are not.
OWNER_STOPS = frozenset({"card", "word", "web", "project", "recall"})

RETIRED = {
    "done": ("已完成", "完成", "✅ 完成", "✅ 已完成"),
    "waiting": ("等你回答", "需要你回答", "❓ 需要你回答"),
    "running": ("进行中",),
    "result": ("有结果未看", "做完没看"),
    "stopped": ("失败", "执行失败", "❌ 失败", "⚠️ 失败", "⚠️ 执行失败", "中断", "已中断"),
}

STOPPED = frozenset({"failed", "interrupted"})
FINISHED = frozenset({"done", "continued"})
RUNS = frozenset({"running", "cancel_requested"})
WAITS = frozenset({"waiting_user", "waiting_workspace"})


def owner_stopped(record) -> bool | None:
    """Whether the owner stopped this cancelled record himself: True, False, or None when the record does not say. One
    taken out of the queue or merged into a running task as a supplement (``never_started``, ``merged_into_job_id``)
    without a ``cancel_requested_by`` is his: only his click on a queued task's card or his message does that."""
    record = record or {}
    by = str(record.get("cancel_requested_by") or "")
    if by:
        return by in OWNER_STOPS
    return True if record.get("never_started") or record.get("merged_into_job_id") else None


def tone(status, *, seen=False, stuck=False, by_owner=None) -> str:
    """The STATES key of a record in ``status``. ``seen``: its result was seen; ``stuck``: it runs but may be stuck
    (core/progress.py maybe_stuck); ``by_owner``: ``owner_stopped`` of a cancelled one, which is 停住了 unless the owner is
    known to have stopped it (the 看板's reading until each stop says who). A status this does not know is idle."""
    if status in WAITS:
        return "waiting"
    if status in RUNS:
        return "stuck" if stuck else "running"
    if status == "queued":
        return "queued"
    if status in FINISHED:
        return "done" if seen else "result"
    if status in STOPPED:
        return "stopped"
    if status == "cancelled":
        return "cancelled" if by_owner else "stopped"
    return "idle"


def record_tone(record, *, seen=False, stuck=False) -> str:
    """``tone`` of a job record."""
    record = record or {}
    return tone(str(record.get("status") or ""), seen=seen, stuck=stuck, by_owner=owner_stopped(record))


def word(key) -> str:
    return STATES[key].word


def group_word(key) -> str:
    return GROUP_WORDS.get(key, STATES[key].word)


def pressing(counts) -> str | None:
    """The most pressing state of ``counts`` ({key: how many}, or an iterable of keys): the first of PRESSING there
    is, None when there is none of them."""
    if not isinstance(counts, dict):
        found: dict = {}
        for key in counts or ():
            found[key] = found.get(key, 0) + 1
        counts = found
    return next((key for key in PRESSING if counts.get(key)), None)


def _rgba(rgb, alpha) -> str:
    return f"rgba({rgb[0]},{rgb[1]},{rgb[2]},{min(alpha, 1.0):.3f})"


def band_alpha(row) -> float:
    """The light mode alpha of the band's row ``row`` (0 at the top)."""
    return max(BAND["floor"], BAND["top"] - BAND["step"] * row)


def band_colors() -> dict:
    """The card colours the band uses (``config.style.color``): ``band-0`` .. ``band-<rows-1>`` and ``band-line``."""
    rgb, dark_rgb, dark = BAND["rgb"], BAND["dark_rgb"], BAND["dark"]
    colors = {f"band-{row}": {"light_mode": _rgba(rgb, band_alpha(row)), "dark_mode": _rgba(dark_rgb, band_alpha(row) * dark)}
              for row in range(BAND["rows"])}
    colors["band-line"] = {"light_mode": _rgba(rgb, BAND["line"][0]), "dark_mode": _rgba(dark_rgb, BAND["line"][1])}
    return colors


def export() -> dict:
    """The table as the web page reads it (look.json): each state's word, group and colours, the order of the most
    pressing, the brand, the band's gradient and the web page's own colours."""
    return {"version": 1,
            "states": {key: {**look._asdict(), "heading": group_word(key)} for key, look in STATES.items()},
            "project": PROJECT._asdict(), "pressing": list(PRESSING), "brand": dict(BRAND),
            "band": {**BAND, "rgb": list(BAND["rgb"]), "dark_rgb": list(BAND["dark_rgb"]), "line": list(BAND["line"])},
            "web": dict(WEB)}
