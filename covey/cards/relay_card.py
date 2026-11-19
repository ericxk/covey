"""The card a topic turn's outcome takes back to the butler's top level (app/butler_relay.py, docs/butler.md
结论回到顶层): ``relay_card``, pure, and what it shares with the question card's copy there
(cards/question_cards.py mirror_card): a topic's reply as card Markdown (``reply_body``)."""
from __future__ import annotations

from covey.cards.answer_card import tail as answer_tail
from covey.cards.elements import card, json_size, md, note
from covey.cards.review import ANSWER_CARD_BYTES, answer_lead, bold_minor_headings, inline_one_line_code
from covey.cards.ui import band_label, banded, conclusion_band, styled
from covey.core import look
from covey.core.failure import HEADLINE_CHARS, split_title
from covey.core.text import literal, redact

CARD_TEXT_BYTES = 24000  # A card is at most 30 KB (CardKit); a longer reply goes as Markdown messages.
QUESTION_CHARS = 300
SUMMARY_CHARS = 40
DONE, WAITING, STOPPED = "done", "waiting", "stopped"


def reply_body(text) -> str:
    """A turn's reply as a card shows it: its line ends as \\n, one-line code inline, its small headings bold."""
    return bold_minor_headings(inline_one_line_code(str(text or "").replace("\r\n", "\n").strip()))


def summary(label, words, text):
    """The card's summary (the chat list's preview): "T410 · 有结果：…"."""
    return f"{label} · {words}：{redact(text)}" if text else f"{label} · {words}"


def relay_card(label, outcome, *, text="", question="", reason="", inputs=None, outputs=(), files=None,
               files_status=None, ask=None, at=None, took=None, artifacts=0, change=None, delivery=""):
    """(card, text): the relay's card, no title bar (None for a reply over CARD_TEXT_BYTES: the Markdown alone), and
    the same as Markdown when the card is refused.
    ``outcome`` DONE (``text`` the saved reply; ``inputs``, ``outputs`` and ``files``, the answer card's 输入/输出
    lines and 产物 panel, cards/answer_card.py, its 输出 ending in ``label``, which ``linked`` makes a link: the
    answer card again, with one link more, 2026-11-08; ``change``, its 改动 fold: a dict, or ``delivery``, the 交付
    line a release before 2026-11-09 kept as a string), WAITING (``question``) or STOPPED (``reason``, one line).
    ``ask``: the "你问" block above it all (a relay at the top level quoting no card, threaded). ``at``, ``took``,
    ``artifacts``: what a relay held (inline_pending) by a release before 2026-11-08 had besides, for a grey line no
    card has now, not shown; without ``inputs`` its box has no 输入 line."""
    head = [ask] if ask else []
    if outcome == DONE:
        body = reply_body(text)
        lead = summary(label, look.word("result"), answer_lead(text, SUMMARY_CHARS))
        if len(body.encode("utf-8")) > CARD_TEXT_BYTES:
            return None, body
        # The conclusion band (docs/ui.md 5.2 聊天结果卡): "有结果 · T410", the reply's lead as its conclusion.
        conclusion = answer_lead(text, HEADLINE_CHARS)
        band = [conclusion_band(band_label("result", label), literal(conclusion))] if conclusion else []
        built = banded(card([*head, *band, md(body), *answer_tail(files, () if inputs is None else inputs, outputs,
                                                                  link=label, change=change or delivery)],
                            lead, update_multi=True))
        # A long 改动 fold may take a card past its limit (as cards/review.py answer_card): the reply goes as
        # Markdown, its parts after it (app/relay_send.py send_once).
        return (built if json_size(built) <= ANSWER_CARD_BYTES else None), body
    elif outcome == WAITING:
        words = " ".join(str(question or "").split())
        body = words[:QUESTION_CHARS] + ("…" if len(words) > QUESTION_CHARS else "")
        # A short question is the band's 要你定 alone; a longer one goes under it whole, never said twice.
        short = len(words) <= HEADLINE_CHARS - 4
        key, headline, line = "waiting", "要你定：" + (words if short else "下面这个问题"), ""
        first = f"**{label} {look.word(key)}**"
        tail = f"在 {label} 的问题卡里答，或在这里回「{label} …」"
        lead = summary(label, look.word(key), words[:SUMMARY_CHARS])
    else:
        why = " ".join(str(reason or "").split())[:80]
        short = False
        key, body, tail = "stopped", "", f"详情在 {label}"
        headline, line = split_title(why) if why else ("没有说原因", "")
        first = f"**{label} {look.word(key)}**" + (f"：{why}" if why else "")
        lead = summary(label, look.word(key), why[:SUMMARY_CHARS])
    band = conclusion_band(band_label(key, label), literal(headline), literal(line))
    shown = body and not (key == "waiting" and short)
    elements = head + [band] + ([md(body)] if shown else []) + [note(tail)]
    markdown = "\n\n".join(part for part in (first, body, tail) if part)
    return styled(card(elements, lead)), markdown
