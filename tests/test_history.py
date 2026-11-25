"""Field history (2026-10-08) — store half, no Home Assistant."""

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

from mission_control.store import MissionStore as Store, StoreError  # noqa: E402

TZ = ZoneInfo("America/Chicago")


class Clock:
    def __init__(self) -> None:
        self.t = dt.datetime(2026, 10, 8, 17, 0, tzinfo=dt.UTC)

    def __call__(self) -> dt.datetime:
        return self.t


def test_every_write_leaves_rows_and_revert_restores(tmp_path: Path) -> None:
    clock = Clock()
    s = Store(tmp_path / "mc.db", TZ, now=clock)
    s.open()
    uid = s.create("projects", "First title", "needs_action", None, "LANE: need\nNEXT STEP: a\n", None, None)
    h = s.history(uid)
    assert [(r["field"], r["source"]) for r in h] == [("created", "todo")]

    s.update_fields(uid, {"title": "Second title", "lane": "want"}, source="ws")
    h = s.history(uid)
    assert {(r["field"], r["old"], r["new"]) for r in h[:2]} == {("title", "First title", "Second title"), ("lane", "need", "want")}
    assert all(r["source"] == "ws" for r in h[:2])

    # a phone edit that changes next step and adds a note
    desc = s.describe(s.get(uid)).replace("NEXT STEP: a", "NEXT STEP: b") + "\n\nPROGRESS:\n2026-10-08 — tried it"
    assert s.update_from_todo(uid, None, None, None, desc)
    h = s.history(uid)
    assert h[0]["field"] == "note" and h[0]["new"] == "2026-10-08 — tried it" and h[0]["source"] == "todo"
    assert h[1]["field"] == "next_step" and (h[1]["old"], h[1]["new"]) == ("a", "b")

    s.add_note(uid, "from Claude", source="ws")
    assert s.history(uid)[0]["source"] == "ws"

    # revert the title change: a new row, the old one stays
    title_row = next(r for r in s.history(uid) if r["field"] == "title")
    s.revert(title_row["id"])
    assert s.get(uid)["title"] == "First title"
    top = s.history(uid)[0]
    assert (top["field"], top["old"], top["new"], top["source"]) == ("title", "Second title", "First title", "revert")
    assert any(r["id"] == title_row["id"] for r in s.history(uid))

    with pytest.raises(StoreError):
        s.revert(top["id"] + 1000)
    with pytest.raises(StoreError):
        s.revert(next(r for r in s.history(uid) if r["field"] == "note")["id"])

    # changed_since sees the card, with its change count
    rows = s.changed_since("2026-10-08T00:00:00+00:00")
    assert rows and rows[0]["uid"] == uid and rows[0]["changes"] >= 6
    assert s.changed_since("2027-01-01T00:00:00+00:00") == []
    assert s.changed_since("2026-10-08T00:00:00+00:00", branch="personal") == []

    # closing and deleting are recorded; export carries the last 90 days
    s.update_fields(uid, {"status": "completed"})
    assert s.history(uid)[0]["field"] == "status"
    s.delete([uid])
    assert s.history(uid)[0]["field"] == "deleted"
    assert any(r["field"] == "deleted" for r in s.export()["history"])
    s.close()


def test_system_sources_for_unblock_and_repeat(tmp_path: Path) -> None:
    clock = Clock()
    s = Store(tmp_path / "mc.db", TZ, now=clock)
    s.open()
    a = s.create("home", "Chore", "needs_action", "2026-10-10", "LANE: need\nREPEAT: every month\nBLOCKED BY: 2026-10-01\nNEXT STEP: x\n", None, None)
    s.resolve_blocks()
    srcs = [(r["field"], r["source"]) for r in s.history(a)]
    assert ("blocked_by", "system:unblock") in srcs and ("note", "system:unblock") in srcs
    s.update_fields(a, {"status": "completed"}, source="ws")
    srcs = [(r["field"], r["source"]) for r in s.history(a)]
    assert ("status", "system:repeat") in srcs and ("due", "ws") in srcs
    s.close()


def test_old_rows_are_pruned_on_open(tmp_path: Path) -> None:
    clock = Clock()
    path = tmp_path / "mc.db"
    s = Store(path, TZ, now=clock)
    s.open()
    uid = s.create("studio", "Old", "needs_action", None, "LANE: want\nNEXT STEP: x\n", None, None)
    s.close()
    clock.t += dt.timedelta(days=200)
    s = Store(path, TZ, now=clock)
    s.open()
    assert s.history(uid) == []
    s.close()
