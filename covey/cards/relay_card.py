"""The card a topic turn's outcome takes back to the butler's top level (app/butler_relay.py, docs/butler.md
结论回到顶层): ``relay_card``, pure, and what it shares with the question card's copy there
(cards/question_cards.py mirror_card): a topic's reply as card Markdown, whole (``reply_body``, ``reply_shown``; both in
cards/review.py, where the answer card in a topic's thread takes them too). ``merged_card``: 做完了 relays that came close together, as one card
(app/relay_send.py send_merged). A 做完了 card ends with a grey line saying where a reply to it goes (``reply_hint``, T647 P1-A3):
to its topic, and on the merged card to the butler (MERGED_HINT). ``relay_pages``: a 做完了 reply too large for one card,
over several (cards/pages.py)."""
from __future__ import annotations

from covey.cards.answer_card import change_element, check_element, details, facts_element, words_fold
from covey.cards.elements import card, element_count, grey, json_size, md, note
from covey.cards.review import ANSWER_CARD_BYTES, FOLD_ID, answer_lead, drop_part_heading, reply_body, reply_shown
from covey.cards.ui import BODY_PADDING, BODY_SPACING, banded, container, speaker_row, split_ask, topic_badge
from covey.core import look, rich_md
from covey.core.card_limits import TARGET_NODES
from covey.core.failure import HEADLINE_CHARS, split_title
from covey.core.text import literal, redact

CARD_TEXT_BYTES = 24000  # A card is at most 30 KB (CardKit); a longer reply goes on several (``relay_pages``).
QUESTION_CHARS = 300
SUMMARY_CHARS = 40
REASON_CHARS = 200  # A stop's reason and next step (core/failure.py failure_view's title), before they are split.
DONE, WAITING, STOPPED = "done", "waiting", "stopped"
NOTE_ID = "relay_note"  # A 停住了 card's last line (``stop_note``), redrawn after a click on its buttons (app/relay_stops.py).
HINT_ID = "relay_hint"  # A 做完了 card's last line (``reply_hint``): its T number stays plain (app/relay_send.py linked).
REPLY_HINT = "回复这张卡，就是接着跟 {} 说"
MERGED_HINT = "回复这张卡是跟管家说；要接着跟某个话题说，点那一行进话题"


def reply_hint(label="", words=""):
    """The grey line that ends a 做完了 relay (HINT_ID): a reply to it goes to its topic as a message in its thread would
    (app/relay_send.py remember_sent), "回复这张卡，就是接着跟 T12 说"; ``words`` in place of that (MERGED_HINT)."""
    said = words or REPLY_HINT.format(label or "这个话题")
    return md(grey(literal(said)), size="notation", element_id=HINT_ID)


def stop_note(label, why=""):
    """The last line of a 停住了 relay (NOTE_ID): "详情在 T439"; after a click that started nothing (the topic had a task at work,
    a result still could not be sent), why, its buttons kept: "没有开始：… · 详情在 T439"."""
    words = f"详情在 {label}"
    why = " ".join(str(why or "").split()).rstrip("。")
    return md(grey(literal(f"没有开始：{why} · {words}" if why else words)), element_id=NOTE_ID, size="notation")


def foot(inputs, outputs, *, change=None):
    """What ends a relay (FACTS_ID, cards/answer_card.py facts_element): its 输入 and 输出 lines and the 改动 line of a
    turn that changed code (a dict); None without them. No link ends it: the row opens the topic."""
    return facts_element(inputs, outputs, change=change) if inputs or outputs or change else None


def into(url, meta) -> str:
    """Where a click on the relay's row goes, into its topic: ``url`` (app/relay_send.py topic_url), else the "你问"
    block's (``meta``, cards/ui.py split_ask): a topic's turn's block opens that topic's thread too, at the same card.
    '' without either."""
    return str(url or "") or str((meta or {}).get("url") or "")


def summary(label, words, text):
    """The card's summary (the chat list's preview): "T41 · 有结果：…"."""
    return f"{label} · {words}：{redact(text)}" if text else f"{label} · {words}"


def relay_card(label, outcome, *, text="", question="", reason="", inputs=None, outputs=(), files=None,
               files_status=None, ask=None, at=None, took=None, artifacts=0, change=None, delivery="", notes=(),
               buttons=(), url="", avatar=""):
    """(card, text): the relay's card (None for a reply over CARD_TEXT_BYTES: it goes on several, ``relay_pages``),
    and the same as Markdown when the card is refused. Its first element says whose it is (cards/ui.py speaker_row: the
    topic's T number as its badge, its title, its state as a tag, what it did with the owner's words after it), so the
    card has no "你问" block. ``avatar``: the speaker's picture (an image key) when ``ask`` has none and no T number
    makes a badge.
    ``outcome`` DONE (``text`` the saved reply; ``inputs`` and ``outputs``, the answer card's 输入/输出 lines, which the
    foot (``foot``) keeps whole, with the 改动 line of ``change``, a dict; ``files``, its 产物 panel,
    cards/answer_card.py; ``delivery``, the 交付
    line an older release kept as a string, ``change`` of those, its fold; ``notes`` and ``buttons``, its other parts, as
    the answer card's tail takes them), WAITING (``question``) or STOPPED (``reason``, one line). A DONE reply is the
    card's body as it is, whole, no conclusion band over it (its first words are its conclusion). A STOPPED ``reason`` is
    core/failure.py failure_view's title: the reason bold under the row and, grey under it, the next step; its ``buttons``
    go under them (app/relay_stops.py area: 检查并继续 and 看原因, redrawn in place once clicked: a shared card, ``update_multi``).
    ``ask``: the "你问" block (cards/answer_card.py ask_block; a relay at the top level quoting no card, threaded):
    who and what it did go to the speaker's row, the owner's words to one line under it that opens (``words_fold``).
    The row is the way into the topic (``into``: ``url``, which opens it at this turn's card, else the block's own
    link, the same thread): a click anywhere on it, no "进话题 ›" at the end (the owner, 2026-11-22). ``at``, ``took``,
    ``artifacts``: what a relay held (inline_pending) by an older release had besides, for a grey line no
    card has now, not shown; without ``inputs`` the foot has no 输入 line."""
    meta, _ = split_ask([ask] if ask else [])
    link = into(url, meta)
    key = {DONE: "result", WAITING: "waiting"}.get(outcome, "stopped")
    row = speaker_row(meta, outcome=key, label=label, avatar=avatar, url=link)
    said = [part for part in (words_fold(meta),) if part]  # What the owner said, one line that opens.
    style = dict(padding=BODY_PADDING, spacing=BODY_SPACING)
    if outcome == DONE:
        body = reply_body(text)
        lead = summary(label, look.word("result"), answer_lead(text, SUMMARY_CHARS))
        if len(body.encode("utf-8")) > CARD_TEXT_BYTES:
            return None, body
        # No conclusion band (T638): the band said the reply's first line again, large, over two rows. The reply as it
        # is, whole (docs/ui.md 5.2 聊天结果卡).
        shown_change = change or delivery
        line = shown_change if isinstance(shown_change, dict) and shown_change else None  # Its 改动 is a line of the foot.
        shown = drop_part_heading(body)  # Its first words say it: no "## 结论" row over them.
        # Whole (the owner, 2026-11-22: 不折叠，直接展示全文); its 这一轮中途的汇报 folded, closed by a click on its text too
        # (app/fold_close.py: the Bridge keeps the card as sent, app/relay_send.py).
        reply = reply_shown(shown, closable=True) if shown.strip() else []
        ending = foot(inputs, outputs, change=line)
        # As the topic's answer card (cards/answer_card.py tail): a commit not found and the criteria not met out on the card,
        # the rest to look up in one fold (an older release's 改动 string among it), then the foot, 换配置 under it, and
        # where a reply goes (T647 P1-A2, A3).
        parts = details([files, change_element(None if line else shown_change), check_element(shown_change), *(notes or ())])
        tail = [*parts, *([ending] if ending else []), *(buttons or ()), reply_hint(label)]
        built = banded(card([row, *said, *reply, *tail], lead, update_multi=True, **style))
        if element_count(built) > TARGET_NODES and rich_md.rich(shown):  # Its 富文本块 in words: one markdown element.
            reply = reply_shown(shown, closable=True, rich=False)
            built = banded(card([row, *said, *reply, *tail], lead, update_multi=True, **style))
        # A long 改动 fold may take a card past its limit (as cards/review.py answer_card): the reply goes on
        # several cards, its parts under the last or after them (``relay_pages``, app/relay_send.py send_once).
        return (built if json_size(built) <= ANSWER_CARD_BYTES else None), body
    elif outcome == WAITING:
        words = " ".join(str(question or "").split())
        body = words[:QUESTION_CHARS] + ("…" if len(words) > QUESTION_CHARS else "")
        # A short question is its bold 要你定 line alone; a longer one goes under a bold 要你定 whole, never said twice.
        short = len(words) <= HEADLINE_CHARS - 4
        key, headline, line = "waiting", "要你定" + (f"：{words}" if short else ""), ""
        first = f"**{label} {look.word(key)}**"
        tail = f"在 {label} 的问题卡里答，或在这里回「{label} …」"
        lead = summary(label, look.word(key), words[:SUMMARY_CHARS])
    else:
        why = " ".join(str(reason or "").split())[:REASON_CHARS]
        short = False
        key, body, tail = "stopped", "", f"详情在 {label}"
        headline, line = split_title(why) if why else ("没有说原因", "")
        first = f"**{label} {look.word(key)}**" + (f"：{why}" if why else "")
        lead = summary(label, look.word(key), headline[:SUMMARY_CHARS])
    # No conclusion band (the owner, 2026-11-23: 这种UI还没改呢, of a band that said its row's 停住了 again): the reason or
    # what is asked bold under the row, what can be done next grey, as a stopped task's card says it (cards/recovery.py).
    said_line = md("\n".join([f"**{literal(headline)}**", *([grey(literal(line))] if line else [])]))
    shown = body and not (key == "waiting" and short)
    acts = list(buttons or ()) if outcome == STOPPED else []  # Its next steps, under the reason (app/relay_stops.py).
    elements = [row, *said, said_line] + ([md(body)] if shown else []) + acts + [
        stop_note(label) if outcome == STOPPED else note(tail)]
    markdown = "\n\n".join(part for part in (first, body, tail) if part)
    return banded(card(elements, lead, update_multi=outcome == STOPPED, **style)), markdown  # The badge's colour.


MERGED_SUBTITLE = "点一行进那个话题"
MERGED_MORE = "还有 {n} 个也做完了：{labels}"  # The relays a merged card has no room for (``merged_card`` ``more``).
ROW_ID = "merged_row"


def files_words(count) -> str:
    """What a merged card's row says of a turn's 产物 (its relay's own card had the panel): "产物 3 个"; '' for none."""
    return f"产物 {count} 个" if isinstance(count, int) and not isinstance(count, bool) and count > 0 else ""


def merged_card(items, avatar="", more=()):
    """(card, text): 做完了 relays that came close together (app/relay_send.py send_merged) as one card, the Markdown when
    the card is refused. ``items``: each relay's {label (T41), title, lead (its reply's first line), url (that opens
    its topic, when known), files (how many 产物, when any)}, in the order they came. The speaker's row (``avatar``, a
    picture when given) says how many (有结果 tag); one block each, the topic's T number as its badge (cards/ui.py
    topic_badge), its title bold and its lead grey beside it with its 产物 counted, the whole block opening ``url``; then
    MERGED_HINT: a reply to this card is the butler's, as it holds several topics (a relay with something the owner must
    see never comes here: app/butler_relay.py relay). ``more``: the relays after them, too many for one card
    (app/relay_send.py take), their T numbers on one grey line under the blocks (MERGED_MORE); counted in the row and
    listed in the Markdown all the same."""
    shown, items = len(items), [*items, *more]
    count = len(items)
    rows = []
    for item in items[:shown]:
        label, title, url = str(item.get("label") or ""), literal(item.get("title") or ""), str(item.get("url") or "")
        badge = topic_badge(label, url=url)  # Its own link too: a click on it would not reach the block's.
        head = title if badge else " ".join(part for part in (literal(label), title) if part)
        lead = " · ".join(part for part in (literal(item.get("lead") or ""), files_words(item.get("files"))) if part)
        words = "\n".join(part for part in (f"**{head}**" if head else "", grey(lead) if lead else "") if part)
        text = md(words or f"**{literal(label) or '话题'}**", size="normal_v2")
        inner = [{"tag": "column_set", "horizontal_spacing": "8px", "columns": [
            {"tag": "column", "width": "auto", "vertical_align": "top", "elements": [badge]},
            {"tag": "column", "width": "weighted", "weight": 1, "vertical_align": "center", "elements": [text]}]}] if badge else [text]
        row = container(inner, background="grey-50", radius="6px", padding="6px 10px 6px 10px", url=url)
        rows.append(row)
    if more:
        rows.append(note(MERGED_MORE.format(n=len(more), labels="、".join(str(item.get("label") or "话题") for item in more))))
    head = speaker_row({"who": f"{count} 个话题做完了", "says": MERGED_SUBTITLE}, outcome="result", avatar=avatar)
    labels = "、".join(str(item.get("label") or "话题") for item in items)
    built = banded(card([head, *rows, reply_hint(words=MERGED_HINT)], summary(f"{count} 个话题", look.word("result"), labels),
                        update_multi=True, padding=BODY_PADDING, spacing=BODY_SPACING))
    lines = [f"**{count} 个话题做完了**"] + [
        "- " + " ".join(part for part in (str(item.get("label") or ""), str(item.get("title") or "")) if part)
        + (f"：{item['lead']}" if item.get("lead") else "")
        + (f"（{files_words(item.get('files'))}）" if files_words(item.get("files")) else "") for item in items] + ["", MERGED_HINT]
    return built, "\n".join(lines)


def relay_pages(label, *, text="", ask=None, url="", avatar="", link=None, finish=None, **facts):
    """A 做完了 relay too large for one card (``relay_card`` gave none, or its card grew past a card's room once its T
    numbers became links, app/relay_send.py) over as many cards as it takes (cards/pages.py Paged): each opens with the
    relay's row (its T number's badge, its title, 有结果, the way into the topic) with "1/3" in it, the owner's words
    that open under the first, the reply cut where it fits, and under the last what ``relay_card`` puts under a reply
    (产物, 改动, 输入/输出…), else, when the last cannot hold it too, ``alone``: the caller sends it on a card of its own.
    ``link`` (text -> text): the reply's T numbers as links, once, before it is cut; ``finish`` (card -> card): the other
    parts', once (cards/pages.py finished). ``facts`` as ``relay_card`` takes them."""
    from covey.cards.answer_card import WORDS_ID
    from covey.cards.pages import Paged, drawer, finished, paged, paged_after

    meta, _ = split_ask([ask] if ask else [])
    # What goes under a reply, drawn by relay_card itself: the same parts as on one card, from one place.
    bare, _ = relay_card(label, DONE, text="", ask=ask, url=url, avatar=avatar, **facts)
    parts = list(((bare or {}).get("body") or {}).get("elements") or [])[1:]  # Its row is drawn again with the page.
    said, tail = finished(finish, [part for part in parts if isinstance(part, dict) and part.get("element_id") == WORDS_ID],
                          [part for part in parts if not (isinstance(part, dict) and part.get("element_id") == WORDS_ID)])
    body = rich_md.card_markdown(drop_part_heading(reply_body(text)))  # Pages are markdown: 富文本块 in words.

    def row(page):
        return speaker_row(meta, outcome="result", label=label, avatar=avatar, url=into(url, meta), page=page)
    # Its 这一轮中途的汇报 is not cut with the reply (the owner, 2026-11-25: T732's long relay had them open): folded under
    # the last card before the parts, as on one card, or atop the caller's card of the parts (``after``); too long for
    # one card, on cards of their own after the reply's, each a fold of its part of them.
    body, stages, fold = stages_of(body, [row("9/9"), *tail])
    body = link(body) if link else body
    lead = summary(label, look.word("result"), answer_lead(text, SUMMARY_CHARS))
    draw = drawer(lead, row=row, words=said, element_id=FOLD_ID, tail=[*([fold] if fold else []), *tail], update_multi=True)
    if stages and fold is None:
        result = paged_after(body, draw, stages, stage_drawer(lead, row=row, tail=tail, update_multi=True))
    else:
        result = paged(body, draw)
    after = (fold,) if fold and result.alone else ()
    # Its parts alone past a card (``bare``), or past the last one: on their own, the caller's.
    return Paged(result.pages, bare is None or result.alone, after)


def stages_of(body, beside=(), *, closable=True, element_id=FOLD_ID) -> tuple:
    """(reply, reports, fold) of a reply's Markdown ``body`` too large for one card: its 这一轮中途的汇报 (cards/review.py
    split_stages) taken out of the text the cards cut, and their fold (``stages_fold``; ``closable`` as on one card)
    when it goes on one card with ``beside`` (what goes there with it), else None: on cards of their own then
    (``stage_drawer``, cards/pages.py paged_after). (``body``, "", None) without any."""
    from covey.cards.pages import fits
    from covey.cards.review import split_stages, stages_fold

    main, stages = split_stages(body)
    if not stages:
        return body, "", None
    fold = stages_fold(stages, stages_id(element_id, closable), closable)
    return main, stages, (fold if fits(card([*beside, fold], update_multi=True)) else None)


def stages_id(element_id, closable) -> str:
    """The element ID of a fold of 这一轮中途的汇报 under ``element_id``'s reply (cards/review.py reply_shown)."""
    from covey.cards.review import STAGES_CLOSABLE, STAGES_SUFFIX

    return element_id + (STAGES_CLOSABLE + "0" if closable else STAGES_SUFFIX)


def stage_drawer(summary, *, row=None, tail=(), update_multi=False, closable=True, element_id=FOLD_ID):
    """``draw`` (cards/pages.py drawer) of the cards that carry 这一轮中途的汇报 too long for one: each its part of them as
    one closed fold (``stages_fold``), ``tail`` under the last."""
    from covey.cards.pages import drawer
    from covey.cards.review import stages_fold

    fold_id = stages_id(element_id, closable)
    return drawer(summary, row=row, tail=tail, update_multi=update_multi,
                  part_element=lambda text: stages_fold(text, fold_id, closable))
