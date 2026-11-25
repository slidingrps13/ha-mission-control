"""The two daily pushes: the morning plan and the evening wrap.

``mission_control.digest`` builds the text and returns ``{title, message}``; it sends
nothing itself, so an automation decides where it goes (see packages/mission_control.yaml).

Morning plan — today's appointments, then up to three things for you: due today
(smallest effort first) → overdue, only on its 1st/3rd/7th/14th/21st day late and
every 30 days after → undated urgent → soonest this week.  Then Tomorrow, Came free
and what the assistant could take.  Evening wrap — what moved today (closed, moved,
came free) from the store's own history, so phone check-offs count, then tomorrow.
"""

from __future__ import annotations

import datetime as dt
import logging
import re

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .calendar_sync import TAG as SYNCED_TAG
from .const import OWNER_ME
from .websocket import owner_of

_LOGGER = logging.getLogger(__name__)
EFFORT_RE = re.compile(r"^(\d+(?:\.\d+)?)(m|h|d)$")
LATE_DAYS = (1, 3, 7, 14, 21)


def _effort_min(v: str | None) -> float:
    m = EFFORT_RE.match((v or "").strip().lower())
    if not m:
        return 9999
    return float(m[1]) * {"m": 1, "h": 60, "d": 480}[m[2]]


def _with(title: str, *bits: str) -> str:
    bits = [b for b in bits if b]
    return f"{title} ({', '.join(bits)})" if bits else title


def _more(items: list[str], n: int) -> str:
    out = " · ".join(items[:n])
    return out + (f" · +{len(items) - n} more" if len(items) > n else "")


async def _events(hass: HomeAssistant, calendar: str | None, today: dt.date) -> tuple[list[str], list[str]]:
    """(today, tomorrow) appointment lines from the calendar, synced card events skipped."""
    if not calendar or hass.states.get(calendar) is None:
        return [], []
    start = dt_util.start_of_local_day(dt_util.now())
    try:
        resp = await hass.services.async_call(
            "calendar",
            "get_events",
            {"entity_id": calendar, "start_date_time": start.isoformat(),
             "end_date_time": (start + dt.timedelta(days=2)).isoformat()},
            blocking=True,
            return_response=True,
        )
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("digest: calendar read failed: %s", err)
        return [], []
    tom = today + dt.timedelta(days=1)
    t_list: list[tuple[str, str]] = []
    m_list: list[tuple[str, str]] = []
    for e in (resp or {}).get(calendar, {}).get("events", []):
        if str(e.get("description") or "").startswith(SYNCED_TAG):
            continue
        st, en = str(e.get("start")), str(e.get("end"))
        allday = len(st) <= 10
        if allday:
            sd, ed = dt.date.fromisoformat(st[:10]), dt.date.fromisoformat(en[:10])
            label = "all day"
            on_t, on_m, key = sd <= today < ed, sd <= tom < ed, "0"
        else:
            local = dt_util.as_local(dt.datetime.fromisoformat(st))
            sd = local.date()
            label = local.strftime("%I:%M %p").lstrip("0").replace(":00 ", " ")
            on_t, on_m, key = sd == today, sd == tom, local.isoformat()
        line = f"{label} {e.get('summary') or '(no title)'}"
        if on_t:
            t_list.append((key, line))
        if on_m:
            m_list.append((key, line))
    return [x[1] for x in sorted(t_list)], [x[1] for x in sorted(m_list)]


def _classify(rows: list[dict], today: dt.date) -> dict[str, list]:
    b: dict[str, list] = {k: [] for k in ("dtoday", "over", "urg", "dtom", "week", "freed", "ctoday", "cover", "curg")}
    for r in rows:
        if r["status"] != "needs_action":
            continue
        for ts, text in r.get("notes") or []:
            if text.startswith("unblocked:") and (today - dt.date.fromisoformat(ts)).days <= 1:
                b["freed"].append(r["title"])
                break
        if (r.get("blocked_by") or "").strip():
            continue
        mine = owner_of(r) == OWNER_ME
        eff = r.get("effort") or ""
        em = _effort_min(eff)
        due = (r.get("due") or "")[:10]
        dd = (dt.date.fromisoformat(due) - today).days if due else None
        if dd == 0:
            b["dtoday" if mine else "ctoday"].append((em, r["title"], eff))
        elif dd is not None and dd < 0:
            late = -dd
            if late in LATE_DAYS or (late >= 30 and late % 30 == 0):
                b["over" if mine else "cover"].append((late, r["title"], eff))
        elif dd is None and (r.get("lane") or "").lower() == "urgent":
            b["urg" if mine else "curg"].append((em, r["title"], eff))
        elif dd == 1 and mine:
            b["dtom"].append((em, r["title"], eff))
        elif dd is not None and dd <= 7 and mine:
            b["week"].append((dd, r["title"], eff))
    return b


async def _module_lines(data, packages: bool, shopping: bool, today: dt.date) -> dict:
    out = {"arriving": [], "delivered": [], "run": "", "soon": [], "bought": []}
    store = data.store
    if packages:
        lst = await data.run(store.order_listing, 3)
        out["arriving"] = [
            (o.get("items") or o.get("store") or "package")
            + (f" ({o['expected_window']})" if o.get("expected_window") else "")
            for o in lst.get("arriving_today", [])
        ]
        out["delivered"] = [o.get("items") or o.get("store") or "package" for o in lst.get("delivered_today", [])]
    if shopping:
        ov = await data.run(store.shop_overview)
        sr = ov.get("suggested_run")
        if sr:
            od = [d["name"] for d in sr.get("due") or [] if d["due_in"] < 0]
            out["run"] = f"Store run: {sr['name']} clears {sr['count']}" + (
                f", {len(od)} overdue: {' · '.join(od[:3])}" if od else ""
            )
        for x in (ov.get("due_soon") or [])[:4]:
            n = int(x.get("due_in") or 0)
            when = f"{-n}d late" if n < 0 else ("today" if n == 0 else "due " + (today + dt.timedelta(days=n)).strftime("%a"))
            out["soon"].append(f"{x['name']} ({when})")
        for b in ov.get("bought_today") or []:
            if b["name"] not in out["bought"]:
                out["bought"].append(b["name"])
    return out


async def async_build(
    hass: HomeAssistant, data, kind: str, calendar: str | None, packages: bool, shopping: bool
) -> dict:
    now = dt_util.now()
    today = now.date()
    rows = await data.run(data.store.items)
    appts, appts_m = await _events(hass, calendar, today)
    mods = await _module_lines(data, packages, shopping, today)
    day = now.strftime("%a %b ") + str(now.day)

    if kind == "morning":
        b = _classify(rows, today)
        picks = [_with(t, e) for _, t, e in sorted(b["dtoday"])]
        for late, t, e in sorted(b["over"], reverse=True):
            picks.append(_with(t, e, f"{late}d late" + (", close or reschedule?" if late >= 14 else "")))
        picks += [_with(t, e) for _, t, e in sorted(b["urg"])]
        picks += [
            _with(t, e, "due " + (today + dt.timedelta(days=d)).strftime("%a")) for d, t, e in sorted(b["week"])
        ]
        tom = appts_m + [_with(t, e) for _, t, e in sorted(b["dtom"])]
        assistant = (
            [t for _, t, _ in sorted(b["ctoday"])]
            + [t for _, t, _ in sorted(b["cover"], reverse=True)]
            + [t for _, t, _ in sorted(b["curg"])]
        )
        lines = [
            " · ".join(appts) if appts else "No appointments.",
            "Do today: " + " · ".join(picks[:3]) if picks else "Nothing due today.",
        ]
        if mods["arriving"]:
            lines.append("Arriving: " + _more(mods["arriving"], 4))
        if mods["run"]:
            lines.append(mods["run"])
        elif mods["soon"]:
            lines.append("Buy soon: " + " · ".join(mods["soon"]))
        if tom:
            lines.append("Tomorrow: " + _more(tom, 3))
        if b["freed"]:
            lines.append("Came free: " + " · ".join(b["freed"][:3]))
        if assistant:
            lines.append("Claude could take: " + " · ".join(assistant[:2]))
        return {"title": f"{day} · your day", "message": "\n".join(lines)}

    act = await data.run(data.store.activity_today)
    closed = [a["title"] for a in act["closed"]]
    moved = [a["title"] for a in act["moved"]]
    freed = [a["title"] for a in act["freed"]]
    tom_s = (today + dt.timedelta(days=1)).isoformat()
    dtom = [
        _with(r["title"], r.get("effort") or "")
        for r in rows
        if r["status"] == "needs_action"
        and (r.get("due") or "")[:10] == tom_s
        and not (r.get("blocked_by") or "").strip()
    ]
    tom = appts_m + dtom
    lines = ["Done: " + _more(closed, 4) if closed else "Nothing closed today."]
    if moved:
        lines.append("Moved: " + _more(moved, 4))
    if freed:
        lines.append("Came free: " + " · ".join(freed[:3]))
    if mods["delivered"]:
        lines.append("Delivered: " + _more(mods["delivered"], 4))
    if mods["bought"]:
        lines.append("Bought: " + _more(mods["bought"], 5))
    lines.append("Tomorrow: " + _more(tom, 3) if tom else "Tomorrow: nothing scheduled.")
    return {"title": f"{day} · what moved", "message": "\n".join(lines)}
