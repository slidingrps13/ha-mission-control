"""Due date → calendar reconcile (2026-10-07) — store half, no HA."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
import sqlite3
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "custom_components" / "mission_control"
if "mission_control" not in sys.modules:
    import types

    pkg = types.ModuleType("mission_control")
    pkg.__path__ = [str(PKG)]
    sys.modules["mission_control"] = pkg

from mission_control.store import SCHEMA, MissionStore as Store  # noqa: E402

TZ = ZoneInfo("America/Chicago")
DESC = "LANE: need\nNEXT STEP: Call them back.\n"


def _store(path: Path) -> Store:
    s = Store(path, TZ, now=lambda: dt.datetime(2026, 10, 7, 12, 0, tzinfo=dt.UTC))
    s.open()
    return s


def test_due_items_lists_only_open_cards_with_a_due(tmp_path: Path) -> None:
    s = _store(tmp_path / "mc.db")
    a = s.create("projects", "Due card", "needs_action", "2026-10-09", DESC, None, None)
    s.create("projects", "No due", "needs_action", None, DESC, None, None)
    c = s.create("personal", "Closed", "completed", "2026-10-09", DESC, None, None)
    items = s.due_items()
    assert [i["uid"] for i in items] == [a]
    assert items[0]["next_step"] == "Call them back."
    assert items[0]["branch"] == "projects"
    s.update_fields(c, {"status": "needs_action"})
    assert {i["uid"] for i in s.due_items()} == {a, c}
    s.update_fields(a, {"due": ""})
    assert [i["uid"] for i in s.due_items()] == [c]
    s.close()


def test_calendar_rows_round_trip(tmp_path: Path) -> None:
    s = _store(tmp_path / "mc.db")
    assert s.calendar_rows() == {}
    s.calendar_set("u1", "ev-1", "2026-10-09", "Due card")
    s.calendar_set("u1", "ev-2", "2026-10-10", "Due card")  # replace, not duplicate
    rows = s.calendar_rows()
    assert rows == {"u1": {"item_uid": "u1", "event_uid": "ev-2", "due": "2026-10-10", "title": "Due card"}}
    s.calendar_del("u1")
    s.calendar_del("u1")  # idempotent
    assert s.calendar_rows() == {}
    s.close()


def test_old_database_gains_the_calendar_table(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    old = SCHEMA[: SCHEMA.index("CREATE TABLE IF NOT EXISTS calendar_events")] + SCHEMA[
        SCHEMA.index("CREATE TABLE IF NOT EXISTS deleted_items") :
    ]
    assert "calendar_events" not in old
    conn = sqlite3.connect(path)
    conn.executescript(old)
    conn.close()
    s = _store(path)
    s.calendar_set("u1", None, "2026-10-09", "t")
    assert s.calendar_rows()["u1"]["event_uid"] is None
    s.close()


def test_event_window_and_description() -> None:
    from custom_components.mission_control import calendar_sync as cs

    d0, d1, all_day = cs.event_window("2026-10-09")
    assert (d0, d1, all_day) == (dt.date(2026, 10, 9), dt.date(2026, 10, 10), True)
    t0, t1, all_day = cs.event_window("2026-10-09T14:30:00")
    assert not all_day and t0.tzinfo is not None and t1 - t0 == dt.timedelta(hours=1)
    desc = cs.event_description(
        {"uid": "83bc0cbe-aaaa-bbbb-cccc-ddddeeeeffff", "branch": "projects", "next_step": "Do it."}
    )
    assert desc == "Mission Control card 83bc0cbe (Projects)\nDo it."
    assert desc.startswith(cs.TAG)
