"""Quick add: turn a plain-language note into one or more lean cards.

``mission_control.file_note`` is the single capture path — the panel's quick-add
box, a voice script, a phone shortcut.  With an AI Task entity configured, the
model picks the branch, lane, toolbox, effort, cost, owner, next step and due
date, splits a note that mixes jobs, and spots a duplicate of an open card.
Without one (or when the model call fails) the note is filed as one plain card,
so nothing is ever lost.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar, device_registry as dr
from homeassistant.util import dt as dt_util

from .const import (
    BRANCH_CONF,
    BRANCHES,
    CLAUDE_BRANCHES,
    OWNER_AI,
    OWNER_ME,
    WORK_BRANCHES,
    entity_id_for,
    owner_key,
)

_LOGGER = logging.getLogger(__name__)

LANES = ("urgent", "work", "need", "want")
TOOLBOXES = (
    "Electrical tools out", "Plumbing tools out", "DIY session", "Attic", "Exterior", "Car",
    "Network closet", "On-site", "Physical checks", "Portals & follow-ups", "Session",
)
BUY_RUNS = ("Hardware store run", "Online order")
EFFORT_RE = re.compile(r"^\d+(\.\d+)?(m|h|d)$")
COST_RE = re.compile(r"^\$\d+(\.\d+)?$")
REPEAT_RE = re.compile(r"^every (\d+ )?(day|week|month|year)s?( from close)?$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DEFAULT_NEXT = "Define the first concrete action."

PROMPT = """You file a quick note into a personal task board (Mission Control). Reply with ONLY a JSON object, no prose:
{{"cards": [ {{card}}, ... ], "duplicate_of": "", "covers": []}}
Each card has keys: title, branch, lane, toolbox, buy_first, effort, cost, owner, next_step, due, parent, waits, tags, repeat.

cards: usually exactly ONE card. Split into 2 (at most 3) only when the note mixes jobs that need different setups or different owners — e.g. a physical install the person does plus software setup the assistant can do alone. The first card is the main job; each later card sets "parent": 0. Several places or items done on the same trip or with the same setup stay ONE card.
branch: one of these keys —
{branches}
lane: urgent only for an undated safety, leak, electrical or money risk that must stay in view until done; a deadline is never a reason for urgent — set due instead | work only on a work branch ({work}) | need for chores and obligations | want for nice-to-haves.
toolbox: {toolboxes} | '<Trade> visit' e.g. 'Plumber visit' | none.
buy_first: '' | {buy} (only if a part must be bought first).
effort: your best estimate of the hands-on time to finish the card (actual working time, not waiting or shipping), always set, as one of 15m, 30m, 45m, 1h, 1.5h, 2h, 3h, 4h, 6h, 1d, 2d, 3d (a day = 8 hours).
cost: your best estimate in {currency} of the hardware, parts and paid services needed to finish, as '$45' (round sensibly, no ranges); '' when nothing has to be bought.
owner: claude only for desk work an AI assistant can finish alone{assistant}; else me.
title: under 70 characters, verb first (Add, Fix, Replace, Decide…), no emoji, no dashes or bracket prefixes; an appointment is 'Mon DD @ H:MM AM: Type (Detail)'. next_step: one concrete sentence, no dashes; when only the person can do it, write it as an instruction to them. Keep every action the note asks for — never drop one. Use what is already installed (below) only to make the step fit the house.
due: ISO date only if the note implies one, never earlier than today ({today}; 'asap' = today), else ''.
parent: null for the first card, 0 for split-off cards.
waits: true for a split-off card that cannot start until the main card is done, else false.
tags: OTHER branch keys the card also belongs to, as a list, when one job genuinely spans branches and is still one card. Never the card's own branch. Usually [].
repeat: '' unless the note describes a recurring chore; then exactly 'every N days|weeks|months|years' (monthly = 'every month', twice a year = 'every 6 months'), adding ' from close' only when the gap counts from when it is done. A repeating card also gets a due date for the first time it is due.

duplicate_of: the 8-character uid of an open card below that is the SAME task as the whole note, else ''.
covers: 8-character uids of OTHER open cards below whose entire task is now included in this note. Usually [].

Already installed in the room(s) the note names:
{devices}

Open cards (uid [branch] title):
{cands}

Note: "{note}"
"""


def _branch_lines() -> str:
    out = []
    for key, name in BRANCHES.items():
        conf = BRANCH_CONF.get(key, {})
        hint = (conf.get("keywords") or "").strip()
        out.append(f"- {key} ({name}{': ' + hint if hint else ''})")
    return "\n".join(out)


def _device_context(hass: HomeAssistant, text: str) -> str:
    low = text.lower()
    areas = ar.async_get(hass)
    devices = dr.async_get(hass)
    lines = []
    for area in areas.async_list_areas():
        name = (area.name or "").lower()
        keys = {name, area.id.replace("_", " "), name.replace("bathroom", "bath")}
        if not any(k and k in low for k in keys):
            continue
        names = [
            d.name_by_user or d.name
            for d in dr.async_entries_for_area(devices, area.id)
            if d.name_by_user or d.name
        ]
        lines.append(f"{area.name}: " + (", ".join(names[:25]) if names else "no devices"))
    return "\n".join(lines) if lines else "(no room named in the note)"


def _parse_json(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    text = str(raw or "")
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        raise ValueError("no JSON object in the model reply")
    return json.loads(text[a : b + 1])


def _default_branch(pins: dict) -> str:
    if pins.get("branch") in BRANCHES:
        return pins["branch"]
    return next(iter(BRANCHES))


def normalize(raw_cards: list, txt: str, pins: dict, today: str) -> list[dict]:
    """Clamp the model's cards to the board's vocabulary (same rules as the panel)."""
    keys = list(BRANCHES)
    out = []
    for n, c0 in enumerate(raw_cards[:3]):
        c = dict(c0) if isinstance(c0, dict) else {}
        if n == 0:
            c.update({k: v for k, v in (pins or {}).items() if v not in (None, "")})
        branch = c.get("branch") if c.get("branch") in keys else _default_branch(pins)
        lane = str(c.get("lane") or "").lower()
        if lane not in LANES:
            lane = "work" if branch in WORK_BRANCHES else "need"
        if lane == "work" and branch not in WORK_BRANCHES:
            lane = "need"
        due = str(c.get("due") or "")
        due = due if DATE_RE.match(due) else ""
        if due and due < today:
            due = today
        tags = c.get("tags") if isinstance(c.get("tags"), list) else []
        tags = [t for t in dict.fromkeys(str(t).lower() for t in tags) if t in keys and t != branch]
        rep = str(c.get("repeat") or "").strip().lower()
        rep = rep if REPEAT_RE.match(rep) else ""
        eff = str(c.get("effort") or "").strip().lower()
        eff = eff if EFFORT_RE.match(eff) else ""
        cost = str(c.get("cost") or "").strip().replace(",", "")
        cost = cost if COST_RE.match(cost) and cost not in ("$0", "$0.00") else ""
        owner = owner_key(c.get("owner")) or OWNER_ME
        toolbox = str(c.get("toolbox") or "").strip()
        if toolbox.lower() == "none":
            toolbox = ""
        buy = str(c.get("buy_first") or "").strip()
        out.append(
            {
                "title": (str(c.get("title") or txt).strip() or txt)[:80],
                "branch": branch,
                "lane": lane,
                "toolbox": toolbox,
                "buy_first": buy,
                "effort": eff,
                "cost": cost,
                "owner": owner,
                "next_step": str(c.get("next_step") or DEFAULT_NEXT).strip() or DEFAULT_NEXT,
                "due": due,
                "tags": tags,
                "repeat": rep,
                "child": n > 0,
                "waits": n > 0 and c.get("waits") in (True, "true", "True", 1),
            }
        )
    return out


def describe(card: dict, src: str, txt: str, today: str, *, parent: str = "", split: str = "") -> str:
    """The lean-card description for a filed card (codec header order)."""
    lines = [f"LANE: {card['lane']}"]
    # OWNER only when it differs from the branch default (the panel infers the rest).
    default_owner = OWNER_AI if card["branch"] in CLAUDE_BRANCHES else OWNER_ME
    if card["owner"] != default_owner:
        lines.append(f"OWNER: {card['owner']}")
    if card["toolbox"]:
        lines.append(f"TOOLBOX: {card['toolbox']}")
    if card["buy_first"]:
        lines.append(f"BUY FIRST: {card['buy_first']}")
    if card["effort"]:
        lines.append(f"EFFORT: {card['effort']}")
    if card["cost"]:
        lines.append(f"COST: {card['cost']}")
    if parent and card.get("waits"):
        lines.append(f"BLOCKED BY: {parent}")
    if parent:
        lines.append(f"PARENT: {parent}")
    if card["tags"]:
        lines.append(f"TAGS: {', '.join(card['tags'])}")
    if card["repeat"]:
        lines.append(f"REPEAT: {card['repeat']}")
    lines.append(f"NEXT STEP: {card['next_step']}")
    return "\n".join(lines) + f"\n\nFiled from a quick note ({src}, {today}{split}): “{txt}”"


def _title_key(title: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", (title or "").lower()).split())


async def async_file_note(
    hass: HomeAssistant,
    data,
    ai_entity: str | None,
    note: str,
    source: str = "panel",
    pins: dict | None = None,
    force: bool = False,
    dry_run: bool = False,
    currency: str = "US dollars",
) -> dict:
    txt = (note or "").strip()
    if not txt:
        raise ValueError("empty note")
    src = source or "voice"
    pins = dict(pins or {})
    store = data.store
    today = dt_util.now().date().isoformat()

    rows = [r for r in await data.run(store.items) if r["status"] == "needs_action"]
    lookup = {r["uid"][:8]: r for r in rows}
    devices = _device_context(hass, txt)

    meta = {"dup": "", "covers": []}
    ai_used = False
    raw_cards: list = [{}]
    if ai_entity and hass.states.get(ai_entity) is not None:
        cands = "\n".join(f"- {r['uid'][:8]} [{r['branch']}] {r['title'][:90]}" for r in rows) or "(none)"
        assist = sorted(CLAUDE_BRANCHES)
        prompt = PROMPT.format(
            branches=_branch_lines(),
            work=", ".join(sorted(WORK_BRANCHES)) or "none",
            toolboxes=" | ".join(TOOLBOXES),
            buy=" | ".join(BUY_RUNS),
            currency=currency,
            assistant=(" on " + ", ".join(assist)) if assist else "",
            today=dt_util.now().strftime("%A %Y-%m-%d"),
            devices=devices,
            cands=cands,
            note=txt.replace('"', "'"),
        )
        try:
            resp = await hass.services.async_call(
                "ai_task",
                "generate_data",
                {"task_name": "mission_control_file_note", "entity_id": ai_entity, "instructions": prompt},
                blocking=True,
                return_response=True,
            )
            j = _parse_json((resp or {}).get("data"))
            cards = j.get("cards")
            raw_cards = cards if isinstance(cards, list) and cards else [j]
            meta["dup"] = str(j.get("duplicate_of") or "")[:8]
            cov = j.get("covers")
            meta["covers"] = [str(u)[:8] for u in cov] if isinstance(cov, list) else []
            ai_used = True
        except Exception as err:  # noqa: BLE001 — any model failure falls back to a plain card
            _LOGGER.warning("quick add: AI filing failed (%s); filing a plain card", err)
            raw_cards = [{}]

    cards = normalize(raw_cards, txt, pins, today)
    if not ai_used and not meta["dup"]:
        # No model to judge duplicates: catch the obvious one, the same title still open.
        same = _title_key(cards[0]["title"])
        for r in rows:
            if _title_key(r["title"]) == same:
                meta["dup"] = r["uid"][:8]
                break
    covers = [
        {"uid": lookup[u]["uid"], "entity_id": entity_id_for(lookup[u]["branch"]), "title": lookup[u]["title"]}
        for u in meta["covers"]
        if u in lookup and u != meta["dup"]
    ]
    base = {"ai": ai_used, "devices": devices, "card": cards[0], "cards": cards}

    if meta["dup"] and meta["dup"] in lookup and not force:
        d = lookup[meta["dup"]]
        res = {
            **base,
            "status": "duplicate",
            "duplicate_title": d["title"],
            "duplicate_entity": entity_id_for(d["branch"]),
            "duplicate_uid": d["uid"],
        }
        if src not in ("panel", "claude") and not dry_run:
            # No screen to ask on (voice, phone): keep the note on the existing card.
            # The panel and an AI chat show the match and let the person decide.
            await data.run(store.add_note, d["uid"], f"also asked ({src}): {txt}", None, "quickadd")
            data.changed(d["branch"])
            res["status"] = "noted_on_duplicate"
        return res
    if dry_run:
        return {**base, "status": "dry_run", "covers": covers}

    filed = []
    uid0 = ""
    n = len(cards)
    for i, c in enumerate(cards):
        split = f", split {i + 1}/{n}" if n > 1 else ""
        desc = describe(c, src, txt, today, parent=uid0[:8] if i else "", split=split)
        uid = await data.run(
            store.create, c["branch"], c["title"], "needs_action", c["due"] or None, desc, None, None, "quickadd"
        )
        if i == 0:
            uid0 = uid
        filed.append({"card": c, "entity_id": entity_id_for(c["branch"]), "uid": uid})
    data.changed(*{c["branch"] for c in cards})
    return {
        **base,
        "status": "filed",
        "entity_id": filed[0]["entity_id"],
        "uid": uid0,
        "cards": filed,
        "covers": covers,
    }
