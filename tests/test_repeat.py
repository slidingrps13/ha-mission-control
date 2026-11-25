"""Recurring cards (2026-10-08) — codec and store, no Home Assistant."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "custom_components" / "mission_control"
if "mission_control" not in sys.modules:
    import types

    pkg = types.ModuleType("mission_control")
    pkg.__path__ = [str(PKG)]
    sys.modules["mission_control"] = pkg

from mission_control import codec  # noqa: E402
from mission_control.store import MissionStore as Store  # noqa: E402

TZ = ZoneInfo("America/Chicago")
TODAY = dt.date(2026, 10, 8)


@pytest.mark.parametrize(
    ("value", "expect"),
    [
        ("every 6 months", {"n": 6, "unit": "month", "from_close": False}),
        ("Every 2 weeks", {"n": 2, "unit": "week", "from_close": False}),
        ("every month from close", {"n": 1, "unit": "month", "from_close": True}),
        ("every 90 days, from close", {"n": 90, "unit": "day", "from_close": True}),
        ("every year", {"n": 1, "unit": "year", "from_close": False}),
        ("monthly", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_repeat(value, expect) -> None:
    assert codec.parse_repeat(value) == expect


def test_add_interval_clamps_month_ends() -> None:
    assert codec.add_interval(dt.date(2026, 1, 31), 1, "month") == dt.date(2026, 2, 28)
    assert codec.add_interval(dt.date(2028, 1, 31), 1, "month") == dt.date(2028, 2, 29)
    assert codec.add_interval(dt.date(2026, 8, 31), 6, "month") == dt.date(2027, 2, 28)
    assert codec.add_interval(dt.date(2026, 10, 8), 1, "year") == dt.date(2027, 10, 8)
    assert codec.add_interval(dt.date(2026, 10, 8), 2, "week") == dt.date(2026, 10, 22)


def test_next_due() -> None:
    six = codec.parse_repeat("every 6 months")
    assert codec.next_due(six, "2027-03-27", TODAY) == "2027-09-27"   # from due, in the future
    assert codec.next_due(six, "2025-01-15", TODAY) == "2027-01-15"   # long overdue rolls forward past today
    assert codec.next_due(six, None, TODAY) == "2027-04-08"            # no due: from today
    assert codec.next_due(codec.parse_repeat("every 2 weeks from close"), "2026-09-01", TODAY) == "2026-10-22"


def _store(path: Path) -> Store:
    s = Store(path, TZ, now=lambda: dt.datetime(2026, 10, 8, 17, 0, tzinfo=dt.UTC))
    s.open()
    return s


def test_closing_a_repeating_card_reopens_it(tmp_path: Path) -> None:
    s = _store(tmp_path / "mc.db")
    desc = "LANE: need\nREPEAT: every 6 months\nNEXT STEP: Replace the filters.\n"
    uid = s.create("home", "RO filters", "needs_action", "2027-03-27", desc, None, None)
    assert s.get(uid)["repeat"] == "every 6 months"
    assert "TAGS" not in s.describe(s.get(uid)) and "REPEAT: every 6 months" in s.describe(s.get(uid))

    # WS / panel close
    s.update_fields(uid, {"status": "completed"})
    r = s.get(uid)
    assert r["status"] == "needs_action" and r["completed_at"] is None
    assert r["due"] == "2027-09-27"
    assert r["notes"][-1] == ("2026-10-08", "done; next due 2027-09-27")

    # phone check-off (update_from_todo) does the same
    assert s.update_from_todo(uid, None, "completed", "2027-09-27", s.describe(r))
    r = s.get(uid)
    assert r["status"] == "needs_action" and r["due"] == "2028-03-27"
    assert len(r["notes"]) == 2

    # clearing REPEAT first closes for good
    s.update_fields(uid, {"repeat": ""})
    s.update_fields(uid, {"status": "completed"})
    r = s.get(uid)
    assert r["status"] == "completed" and r["completed_at"] is not None and r["due"] == "2028-03-27"
    s.close()


def test_invalid_repeat_is_kept_as_text_and_does_not_reopen(tmp_path: Path) -> None:
    s = _store(tmp_path / "mc.db")
    uid = s.create("personal", "Odd", "needs_action", None, "LANE: want\nREPEAT: monthly-ish\nNEXT STEP: x\n", None, None)
    assert s.get(uid)["repeat"] == "monthly-ish"  # lint flags it; nothing is lost
    s.update_fields(uid, {"status": "completed"})
    assert s.get(uid)["status"] == "completed"
    s.close()
