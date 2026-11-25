"""Auto-unblock (2026-10-08) — codec and store, no Home Assistant."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
import sqlite3
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

from mission_control.codec import parse_blocks  # noqa: E402
from mission_control.store import SCHEMA, MissionStore as Store, StoreError  # noqa: E402

TZ = ZoneInfo("America/Chicago")


@pytest.mark.parametrize(
    ("value", "expect"),
    [
        ("3e150008", [("uid", "3e150008")]),
        ("4766fcc4, 2026-10-20", [("uid", "4766fcc4"), ("date", "2026-10-20")]),
        ("Lenovo tech callback at 555-010-0199", [("text", "Lenovo tech callback at 555-010-0199")]),
        ("2026-09-28 (24 h of fan history after the change on card 88d5af0a)", [("date", "2026-09-28")]),
        ("HA 2026.11 installed", [("text", "HA 2026.11 installed")]),
        ("card 0983a7d4 and the spare cameras plugged in", [("uid", "0983a7d4"), ("text", "the spare cameras plugged in")]),
        ("2026-13-45", [("text", "2026-13-45")]),  # not a real date → a human reads it
        ("", []),
        (None, []),
    ],
)
def test_parse_blocks(value, expect) -> None:
    assert [(b["kind"], b["value"]) for b in parse_blocks(value)] == expect


class Clock:
    def __init__(self) -> None:
        self.t = dt.datetime(2026, 10, 8, 12, 0, tzinfo=dt.UTC)

    def __call__(self) -> dt.datetime:
        return self.t


def _store(path: Path, clock: Clock) -> Store:
    s = Store(path, TZ, now=clock)
    s.open()
    return s


def _card(blocked: str, next_step: str = "Do it.") -> str:
    return f"LANE: need\nBLOCKED BY: {blocked}\nNEXT STEP: {next_step}\n"


def test_uid_block_clears_when_the_target_closes(tmp_path: Path) -> None:
    clock = Clock()
    s = _store(tmp_path / "mc.db", clock)
    blocker = s.create("projects", "Blocker", "needs_action", None, "LANE: need\nNEXT STEP: x\n", None, None)
    blocked = s.create("home", "Blocked", "needs_action", None, _card(blocker[:8]), None, None)
    assert s.is_blocked(blocked)
    assert s.blocks_of(blocked)[0]["target_uid"] == blocker
    assert s.resolve_blocks() == []  # nothing satisfied yet
    assert s.is_blocked(blocked)

    s.update_fields(blocker, {"status": "completed"})
    freed = s.resolve_blocks()
    assert [f["uid"] for f in freed] == [blocked]
    assert freed[0]["reasons"] == [f"card {blocker[:8]} closed"]
    row = s.get(blocked)
    assert row["blocked_by"] is None
    assert row["notes"][-1] == ("2026-10-08", f"unblocked: card {blocker[:8]} closed")
    assert "BLOCKED BY" not in s.describe(row)
    assert not s.is_blocked(blocked)
    assert s.resolve_blocks() == []  # idempotent
    s.close()


def test_date_block_clears_the_day_it_passes(tmp_path: Path) -> None:
    clock = Clock()
    s = _store(tmp_path / "mc.db", clock)
    uid = s.create("personal", "Wait", "needs_action", None, _card("2026-10-10 (two days of history)"), None, None)
    assert s.resolve_blocks() == []
    clock.t = dt.datetime(2026, 10, 10, 6, 0, tzinfo=dt.UTC)  # 01:00 house time on the 10th
    freed = s.resolve_blocks()
    assert [f["uid"] for f in freed] == [uid]
    assert s.get(uid)["notes"][-1][1] == "unblocked: 2026-10-10 passed"
    s.close()


def test_text_block_only_clears_by_hand_and_mixed_stays_blocked(tmp_path: Path) -> None:
    clock = Clock()
    s = _store(tmp_path / "mc.db", clock)
    blocker = s.create("projects", "Blocker", "completed", None, "LANE: need\nNEXT STEP: x\n", None, None)
    uid = s.create("projects", "Mixed", "needs_action", None, _card(f"{blocker[:8]} and door sensor delivered"), None, None)
    freed = s.resolve_blocks()
    assert freed == []  # the uid cleared, the text block still holds the card
    blocks = s.blocks_of(uid)
    assert blocks[0]["kind"] == "uid" and blocks[0]["cleared_at"] is not None
    assert blocks[1]["kind"] == "text" and blocks[1]["cleared_at"] is None
    assert s.is_blocked(uid)
    assert s.get(uid)["blocked_by"].startswith(blocker[:8])  # the line is untouched

    with pytest.raises(StoreError):
        s.clear_block(uid, 0)  # already cleared
    got = s.clear_block(uid, 1, by="me")
    assert got and got["uid"] == uid
    assert got["reasons"] == [f"card {blocker[:8]} closed", "cleared by me"]
    assert s.get(uid)["blocked_by"] is None
    s.close()


def test_cleared_state_survives_a_rewrite_of_the_line(tmp_path: Path) -> None:
    clock = Clock()
    s = _store(tmp_path / "mc.db", clock)
    uid = s.create("lab", "Two", "needs_action", None, _card("2026-01-01 and the rack delivered"), None, None)
    s.resolve_blocks()  # the date is long past → cleared, text holds
    assert s.blocks_of(uid)[0]["cleared_at"] is not None
    s.update_fields(uid, {"next_step": "Changed."})  # any write rebuilds the index
    assert s.blocks_of(uid)[0]["cleared_at"] is not None
    assert s.is_blocked(uid)
    s.update_fields(uid, {"blocked_by": "the rack delivered"})  # date dropped by hand
    assert [b["kind"] for b in s.blocks_of(uid)] == ["text"]
    s.close()


def test_closed_cards_are_left_alone(tmp_path: Path) -> None:
    clock = Clock()
    s = _store(tmp_path / "mc.db", clock)
    uid = s.create("studio", "Old", "completed", None, _card("2020-01-01"), None, None)
    assert s.resolve_blocks() == []
    assert s.get(uid)["blocked_by"] == "2020-01-01"
    s.close()


def test_old_database_gains_the_blocks_table_and_indexes_existing_cards(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    old = SCHEMA[: SCHEMA.index("CREATE TABLE IF NOT EXISTS blocks")] + SCHEMA[
        SCHEMA.index("CREATE TABLE IF NOT EXISTS calendar_events") :
    ]
    conn = sqlite3.connect(path)
    conn.executescript(old)
    conn.close()
    s = _store(path, Clock())
    uid = s.create("projects", "New", "needs_action", None, _card("2026-01-01"), None, None)
    assert s.blocks_of(uid)[0]["kind"] == "date"
    s.close()


def test_open_indexes_existing_blocked_cards_once(tmp_path: Path) -> None:
    clock = Clock()
    path = tmp_path / "mc.db"
    s = _store(path, clock)
    uid = s.create("projects", "Pre", "needs_action", None, _card("2026-01-01"), None, None)
    s.conn.execute("DELETE FROM blocks")  # as if written before the table existed
    s.close()
    s = _store(path, clock)
    assert [b["kind"] for b in s.blocks_of(uid)] == ["date"]
    s.resolve_blocks()
    assert s.get(uid)["blocked_by"] is None
    s.close()


def test_all_digit_uid_prefix_is_a_card_only_when_one_exists(tmp_path: Path) -> None:
    """~2 % of uuid1 uids start with 8 digits; those must still work as card blocks,
    while a bare 8-digit number that names no card stays part of the text."""
    s = _store(tmp_path / "mc.db", Clock())
    blocker = s.create("projects", "Blocker", "needs_action", None, "LANE: need\nNEXT STEP: x\n", "12345678-0000-1000-8000-000000000001", None)
    uid = s.create("projects", "Waits", "needs_action", None, _card("12345678 and call 87654321 back"), None, None)
    kinds = [(b["kind"], b["value"]) for b in s.blocks_of(uid)]
    assert kinds[0] == ("uid", "12345678")
    assert kinds[1][0] == "text"
    s.update_fields(blocker, {"status": "completed"})
    s.resolve_blocks()
    assert s.blocks_of(uid)[0]["cleared_at"] is not None
    s.close()
