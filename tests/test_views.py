"""Derived lists (2026-10-08) — the store query half."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "custom_components" / "mission_control"
if "mission_control" not in sys.modules:
    import types

    pkg = types.ModuleType("mission_control")
    pkg.__path__ = [str(PKG)]
    sys.modules["mission_control"] = pkg

from mission_control.store import MissionStore as Store  # noqa: E402

TZ = ZoneInfo("America/Chicago")
NOW = dt.datetime(2026, 10, 8, 17, 0, tzinfo=dt.UTC)  # 2026-10-08 house time


def owner_of(row: dict) -> str:  # the websocket rule, reduced to what the test needs
    if row.get("owner") in ("me", "claude"):
        return row["owner"]
    return "claude" if row["branch"] in {"projects", "lab", "studio"} else "me"


def test_views(tmp_path: Path) -> None:
    s = Store(tmp_path / "mc.db", TZ, now=lambda: NOW)
    s.open()
    mk = lambda b, t, desc, due=None, st="needs_action": s.create(b, t, st, due, desc, None, None)  # noqa: E731
    a = mk("home", "Want for me", "LANE: want\nNEXT STEP: a\n")
    b = mk("home", "Urgent for me", "LANE: urgent\nNEXT STEP: b\n", "2026-10-12")
    c = mk("projects", "Claude's", "LANE: need\nNEXT STEP: c\n", "2026-10-09")
    d = mk("projects", "Pinned to me", "LANE: need\nOWNER: me\nNEXT STEP: d\n")
    e = mk("personal", "Blocked", "LANE: need\nBLOCKED BY: the part delivered\nNEXT STEP: e\n", "2026-10-10")
    f = mk("personal", "Overdue", "LANE: need\nNEXT STEP: f\n", "2026-10-01")
    g = mk("personal", "Far out", "LANE: need\nNEXT STEP: g\n", "2026-11-01")
    mk("personal", "Closed", "LANE: need\nNEXT STEP: h\n", "2026-10-09", st="completed")

    ready = [r["uid"] for r in s.view_rows("ready_for_you", owner_of)]
    assert ready == [b, f, g, d, a]  # urgent first; need by due (undated last); want last; blocked and Claude's out

    week = [r["uid"] for r in s.view_rows("this_week", owner_of)]
    assert week == [f, c, e, b]  # overdue first, then by due date; far-out and closed out

    # today (PARSING.md §25): overdue → due today → undated urgent; blocked, dated-urgent (b), future out
    u = mk("projects", "Undated urgent", "LANE: urgent\nNEXT STEP: u\n")
    t0 = mk("home", "Due today", "LANE: want\nNEXT STEP: t\n", "2026-10-08")
    f2 = mk("personal", "Older overdue", "LANE: need\nNEXT STEP: f2\n", "2026-09-20")
    rows = s.view_rows("today", owner_of)
    assert [r["uid"] for r in rows] == [f2, f, t0, u]
    assert [r["view_label"] for r in rows] == ["Overdue 18d", "Overdue 7d", "Due today", "Urgent"]
    s.close()
