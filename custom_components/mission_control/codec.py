"""Lean-card codec: parse a tracker description into columns and render it back.

Pure Python, no Home Assistant imports, so the importer, the projection and the
offline tests all share one implementation.  The contract is docs/CARD_FORMAT.md (The lean card, Whose move):

    LANE: …            (header lines, one per line, in this order)
    OWNER: …
    TOOLBOX: …
    BUY FIRST: …
    EFFORT: …          (added 2026-10-11 — hands-on time, e.g. 30m, 2h, 1d)
    COST: …            (added 2026-10-11 — hardware + paid services, e.g. $120; empty = nothing to buy)
    BLOCKED BY: …
    PARENT: …
    TAGS: …            (added 2026-10-07 — other branches this card belongs to)
    REFS: …
    SDP_ID: …
    SOURCE: …
    PIN: …
    NEXT STEP: …

    <free-text details>

    PROGRESS:
    YYYY-MM-DD — note
    YYYY-MM-DD — note

Parsing is lenient and never drops text: anything it does not recognise ends up
in ``details`` verbatim.  Rendering is canonical.  ``classify`` tells the
importer whether a description survives parse → render byte for byte.
"""

from __future__ import annotations

from collections import Counter
import datetime as dt
from dataclasses import dataclass, field
import re

# (header label, column) in lean-card order.
FIELDS: tuple[tuple[str, str], ...] = (
    ("LANE", "lane"),
    ("OWNER", "owner"),
    ("TOOLBOX", "toolbox"),
    ("BUY FIRST", "buy_first"),
    ("EFFORT", "effort"),
    ("COST", "cost"),
    ("BLOCKED BY", "blocked_by"),
    ("PARENT", "parent_uid"),
    ("TAGS", "tags"),
    ("REPEAT", "repeat"),
    ("REFS", "refs"),
    ("SDP_ID", "sdp_id"),
    ("SOURCE", "source"),
    ("PIN", "pin"),
    ("NEXT STEP", "next_step"),
)
COLUMNS: tuple[str, ...] = tuple(c for _, c in FIELDS)
_LABEL_TO_COL = dict(FIELDS)
_HDR = re.compile(
    r"^(" + "|".join(re.escape(lbl) for lbl, _ in FIELDS) + r"):(?: (.*)|)$"
)
_NOTE = re.compile(r"^(\d{4}-\d{2}-\d{2}) — (.*)$")
PROGRESS_MARK = "PROGRESS:"
NOTE_SEP = " — "


@dataclass
class Card:
    """Columns of one card (everything except title/status/due)."""

    headers: dict[str, str] = field(default_factory=dict)  # column -> value
    details: str = ""
    notes: list[tuple[str, str]] = field(default_factory=list)  # (YYYY-MM-DD, text)


def parse(desc: str | None) -> Card:
    """Split a description into header columns, details and dated notes."""
    card = Card()
    if not desc:
        return card
    lines = desc.split("\n")
    i = 0
    while i < len(lines):
        m = _HDR.match(lines[i])
        if not m:
            break
        col = _LABEL_TO_COL[m.group(1)]
        if col in card.headers:  # a repeated field ends the header block
            break
        card.headers[col] = (m.group(2) or "").rstrip()
        i += 1
    rest = lines[i:]

    # The PROGRESS block is the LAST bare "PROGRESS:" line.
    p = None
    for j in range(len(rest) - 1, -1, -1):
        if rest[j].strip() == PROGRESS_MARK:
            p = j
            break
    detail_lines = rest if p is None else rest[:p]
    trailer: list[str] = []
    if p is not None:
        gap = False
        for ln in rest[p + 1 :]:
            m = _NOTE.match(ln)
            if m and not trailer:
                card.notes.append((m.group(1), m.group(2)))
                gap = False
            elif not ln.strip():
                gap = True
                if trailer:
                    trailer.append(ln)
            elif card.notes and not gap and not trailer:
                ts, text = card.notes[-1]
                card.notes[-1] = (ts, text + "\n" + ln)  # multi-line note
            else:
                trailer.append(ln)
    if p is None:
        # No PROGRESS block: a trailing run of dated lines is the note log
        # (two live cards were written that way before the lean card).
        k = len(detail_lines)
        while k > 0 and (not detail_lines[k - 1].strip() or _NOTE.match(detail_lines[k - 1])):
            k -= 1
        run = [ln for ln in detail_lines[k:] if ln.strip()]
        if run:
            card.notes = [(m.group(1), m.group(2)) for m in map(_NOTE.match, run)]
            detail_lines = detail_lines[:k]
    details = "\n".join(detail_lines).strip("\n")
    tail = "\n".join(trailer).strip("\n")
    if tail:
        details = (details + "\n\n" + tail) if details else tail
    card.details = details
    return card


def render(card: Card) -> str | None:
    """Render columns back into the canonical lean-card description."""
    head = []
    for label, col in FIELDS:
        val = card.headers.get(col)
        if val is None:
            continue
        head.append(f"{label}: {val}" if val != "" else f"{label}:")
    blocks = []
    if head:
        blocks.append("\n".join(head))
    if card.details:
        blocks.append(card.details)
    if card.notes:
        blocks.append(
            PROGRESS_MARK
            + "\n"
            + "\n".join(f"{ts}{NOTE_SEP}{text}" for ts, text in card.notes)
        )
    if not blocks:
        return None
    return "\n\n".join(blocks)


def _content_lines(text: str | None) -> Counter:
    return Counter(
        ln.rstrip()
        for ln in (text or "").split("\n")
        if ln.strip() and ln.strip() != PROGRESS_MARK
    )


def classify(desc: str | None) -> tuple[str, Card]:
    """Return ("exact" | "normalized" | "raw", parsed card).

    exact       parse → render reproduces the description byte for byte.
    normalized  only blank lines, trailing spaces or line order differ; every
                non-blank line survives.  Listed for review before import.
    raw         anything else; the importer stores the text verbatim.
    """
    card = parse(desc)
    out = render(card)
    if out == desc or (not desc and out is None):
        return "exact", card
    if _content_lines(out) == _content_lines(desc):
        return "normalized", card
    return "raw", card


def note_line(ts: str, text: str) -> str:
    return f"{ts}{NOTE_SEP}{text}"


# TAGS values are branch keys; a branch's name (and its key with spaces) is
# accepted on read too.  Filled from the configured branches by set_tag_aliases().
TAG_ALIASES: dict[str, str] = {}


def set_tag_aliases(branches: list[dict]) -> None:
    TAG_ALIASES.clear()
    for b in branches:
        key = b["key"]
        for alias in (key, key.replace("_", " "), (b.get("name") or "").strip().lower()):
            if alias:
                TAG_ALIASES.setdefault(alias.replace("-", "_"), key)


def parse_tags(value: str | None, home_branch: str | None = None) -> list[str]:
    """Branch keys named by a TAGS header value, in order, deduplicated.

    Unknown words are ignored (they stay in the header text untouched); the
    card's own home branch is never a tag of itself.
    """
    out: list[str] = []
    for raw in re.split(r"[,;/]+", value or ""):
        key = TAG_ALIASES.get(raw.strip().lower().replace("-", "_"))
        if key and key != home_branch and key not in out:
            out.append(key)
    return out


# --------------------------------------------------------------- BLOCKED BY
# Card 3a2bd75a (2026-10-08): a BLOCKED BY line is a list of blocks of three
# kinds — a card uid (cleared when that card closes), an ISO date (cleared when
# it passes) and free text (an observable event only a human can clear).
_BLOCK_UID = re.compile(r"\b([0-9a-f]{8})(?:-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})?\b")
_BLOCK_DATE = re.compile(r"\b(20\d\d-\d\d-\d\d)\b")
_BLOCK_PAREN = re.compile(r"\([^()]*\)")
_BLOCK_FILLER = re.compile(
    r"\b(and|or|then|card|cards|until|after|once|when|the|a|an|of|on|by|to|is|are|was|were|it|its|this|that|done|complete|completed|closes|closed|finished)\b",
    re.IGNORECASE,
)


def parse_blocks(value: str | None, is_card=None) -> list[dict]:
    """Split a BLOCKED BY value into blocks: ``[{"kind","value"}, …]``.

    Parentheticals are explanations, not blocks ("2026-10-14 (two weeks of
    history)" is one date block).  Anything left after removing uids, dates,
    parentheticals and filler words is one text block, kept in the author's
    words.  Order: uids, dates, then the text block.

    An 8-digit token is ambiguous (an order or phone number looks the same), so
    it counts as a card uid only when ``is_card(token)`` says a card starts with
    it — about 2 % of card uids are all digits.
    """
    text = (value or "").strip()
    if not text:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    base = _BLOCK_PAREN.sub(" ", text)  # uids/dates inside (…) are explanation
    for m in _BLOCK_UID.finditer(base):
        tok = m.group(1)
        if tok in seen or (tok.isdigit() and not (is_card and is_card(tok))):
            continue
        seen.add(tok)
        out.append({"kind": "uid", "value": tok})
    rest = _BLOCK_UID.sub(" ", base)
    for m in _BLOCK_DATE.finditer(base):
        d = m.group(1)
        try:
            dt.date.fromisoformat(d)
        except ValueError:
            continue  # not a real date: it stays in the text for a human
        if d in seen:
            continue
        seen.add(d)
        out.append({"kind": "date", "value": d})
        rest = rest.replace(d, " ")
    probe = _BLOCK_FILLER.sub(" ", rest)
    if re.search(r"[A-Za-z0-9]", probe):
        clean = re.sub(r"\s+", " ", rest).strip(" ,;:/&.-")
        clean = re.sub(r"^(?:(?:and|or|then|card|cards)\s+)+", "", clean, flags=re.IGNORECASE)
        out.append({"kind": "text", "value": clean or text})
    return out


# -------------------------------------------------------------------- REPEAT
# Card 3a389634 (2026-10-08): ``REPEAT: every 6 months`` (also days / weeks /
# years, "every month", "every 2 weeks"); add "from close" to count from the
# day the card is closed instead of from its due date.
_REPEAT = re.compile(
    r"^\s*every\s+(?:(\d+)\s+)?(day|week|month|year)s?(?:\s*[,;]?\s*(from\s+(?:close|closing|done|completion)))?\s*$",
    re.IGNORECASE,
)


def parse_repeat(value: str | None) -> dict | None:
    """``{"n": 6, "unit": "month", "from_close": False}`` or None when not valid."""
    m = _REPEAT.match(value or "")
    if not m:
        return None
    return {
        "n": int(m.group(1) or 1),
        "unit": m.group(2).lower(),
        "from_close": bool(m.group(3)),
    }


def add_interval(day: dt.date, n: int, unit: str) -> dt.date:
    """Calendar arithmetic with day clamping (Jan 31 + 1 month = Feb 28/29)."""
    if unit == "day":
        return day + dt.timedelta(days=n)
    if unit == "week":
        return day + dt.timedelta(weeks=n)
    months = n * (12 if unit == "year" else 1)
    y, m0 = divmod(day.month - 1 + months, 12)
    year, month = day.year + y, m0 + 1
    last = (dt.date(year + (month // 12), month % 12 + 1, 1) - dt.timedelta(days=1)).day
    return dt.date(year, month, min(day.day, last))


def next_due(repeat: dict, due: str | None, today: dt.date) -> str:
    """The next due date after closing a repeating card today.

    From the due date by default (a late close does not drift the schedule);
    from today with "from close" or when the card had no due date.  Rolls
    forward until the result is after today, so a long-overdue card does not
    reopen already overdue.
    """
    base = today
    if not repeat["from_close"] and due:
        try:
            base = dt.date.fromisoformat(due[:10])
        except ValueError:
            base = today
    nxt = add_interval(base, repeat["n"], repeat["unit"])
    while nxt <= today:
        nxt = add_interval(nxt, repeat["n"], repeat["unit"])
    return nxt.isoformat()
