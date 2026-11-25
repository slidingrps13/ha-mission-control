"""Activity today (the two-push digest, 2026-11-02) — the store query half."""

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


class Clock:
    def __init__(self, t: dt.datetime) -> None:
        self.t = t

    def __call__(self) -> dt.datetime:
        return self.t


def test_activity_today(tmp_path: Path) -> None:
    clock = Clock(dt.datetime(2026, 11, 2, 3, 0, tzinfo=dt.UTC))  # Nov 1, 22:00 house time
    s = Store(tmp_path / "mc.db", TZ, now=clock)
    s.open()
    mk = lambda b, t, desc="LANE: need\nNEXT STEP: x\n": s.create(b, t, "needs_action", None, desc, None, None)  # noqa: E731
    yesterday_card = mk("home", "Old card")
    closed_yesterday = mk("home", "Closed yesterday")
    s.update_fields(closed_yesterday, {"status": "completed"}, source="todo")
    # roll past local midnight: Nov 2, 09:00 house time
    clock.t = dt.datetime(2026, 11, 2, 15, 0, tzinfo=dt.UTC)
    a = mk("projects", "Made today")
    s.update_fields(yesterday_card, {"status": "completed"}, source="todo")   # phone check-off, no note
    b = mk("personal", "Noted today")
    clock.t = dt.datetime(2026, 11, 2, 15, 1, tzinfo=dt.UTC)
    s.add_note(b, "did a thing", source="ws")
    c = mk("personal", "Reopened today")
    s.update_fields(c, {"status": "completed"}, source="todo")
    s.update_fields(c, {"status": "needs_action"}, source="todo")

    act = s.activity_today()
    assert act["day"] == "2026-11-02"
    assert [x["title"] for x in act["closed"]] == ["Old card"]            # not the one closed yesterday
    assert {x["title"] for x in act["created"]} == {"Made today", "Noted today", "Reopened today"}
    assert act["moved"] == []                                              # created-today cards are not "moved"
    d = mk("studio", "Edited only")
    clock.t = dt.datetime(2026, 11, 2, 15, 2, tzinfo=dt.UTC)
    s.update_fields(d, {"lane": "want"}, source="panel")
    act = s.activity_today()
    assert [x["title"] for x in act["moved"]] == []                        # still created today
    s.close()


def test_activity_moved_and_freed(tmp_path: Path) -> None:
    clock = Clock(dt.datetime(2026, 11, 1, 15, 0, tzinfo=dt.UTC))
    s = Store(tmp_path / "mc.db", TZ, now=clock)
    s.open()
    e = s.create("home", "Edited", "needs_action", None, "LANE: need\nNEXT STEP: x\n", None, None)
    blocker = s.create("home", "Blocker", "needs_action", None, "LANE: need\nNEXT STEP: y\n", None, None)
    w = s.create("home", "Waiting", "needs_action", None, f"LANE: need\nBLOCKED BY: {blocker[:8]}\nNEXT STEP: z\n", None, None)
    clock.t = dt.datetime(2026, 11, 2, 15, 0, tzinfo=dt.UTC)  # next day
    s.update_fields(e, {"lane": "want"}, source="panel")
    s.update_fields(blocker, {"status": "completed"}, source="todo")
    s.resolve_blocks()
    act = s.activity_today()
    assert [x["title"] for x in act["moved"]] == ["Edited"]
    assert [x["title"] for x in act["closed"]] == ["Blocker"]
    assert [x["title"] for x in act["freed"]] == ["Waiting"]
    s.close()
