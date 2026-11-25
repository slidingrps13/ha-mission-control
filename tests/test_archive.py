"""Archive window for the to-do projection (2026-10-08)."""

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
NOW = dt.datetime(2026, 10, 8, 12, 0, tzinfo=dt.UTC)


def test_projected_keeps_open_recent_and_a_floor_of_closed(tmp_path: Path) -> None:
    s = Store(tmp_path / "mc.db", TZ, now=lambda: NOW)
    s.open()
    open_uid = s.create("projects", "Open", "needs_action", None, "LANE: need\nNEXT STEP: x\n", None, None)
    closed = []
    for days_ago in (1, 10, 29, 31, 60, 200):
        when = (NOW - dt.timedelta(days=days_ago)).isoformat(timespec="seconds")
        closed.append((days_ago, s.create("projects", f"Closed {days_ago}d", "completed", None, "d", None, when)))
    other = s.create("personal", "Other branch", "completed", None, "d", None, (NOW - dt.timedelta(days=2)).isoformat())

    got = {r["uid"] for r in s.projected("projects", window_days=30, floor=2)}
    assert open_uid in got and other not in got
    assert {u for d, u in closed if d <= 30} <= got          # inside the window
    assert {u for d, u in closed if d > 30}.isdisjoint(got)  # outside it, floor already met

    got = {r["uid"] for r in s.projected("projects", window_days=0, floor=4)}
    assert got == {open_uid} | {u for d, u in closed if d in (1, 10, 29, 31)}  # floor wins when the window is empty

    assert len(s.items("projects")) == 7  # the store itself keeps everything
    assert sum(1 for r in s.projected("projects") if r["status"] == "needs_action") == 1
    s.close()
