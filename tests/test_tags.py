"""TAGS header (2026-10-07) — codec and store, no Home Assistant needed."""

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
    # Load codec/store without running the package __init__ (which needs HA).
    import types

    pkg = types.ModuleType("mission_control")
    pkg.__path__ = [str(PKG)]
    sys.modules["mission_control"] = pkg

from mission_control import codec  # noqa: E402
from mission_control.store import ITEM_COLS, SCHEMA, MissionStore as Store  # noqa: E402

TZ = ZoneInfo("America/Chicago")

CARD = (
    "LANE: need\n"
    "TOOLBOX: Attic\n"
    "PARENT: 0983a7d4-c1c9-11f1-a2fd-000000000002\n"
    "TAGS: home, lab\n"
    "REFS: memory project_x\n"
    "NEXT STEP: Do the thing.\n"
    "\n"
    "Some details.\n"
    "\n"
    "PROGRESS:\n"
    "2026-10-07 — created"
)


def test_codec_tags_is_a_header_between_parent_and_refs() -> None:
    labels = [lbl for lbl, _ in codec.FIELDS]
    assert labels.index("PARENT") + 1 == labels.index("TAGS") < labels.index("REFS")  # REPEAT sits between since 2026-10-08
    kind, card = codec.classify(CARD)
    assert kind == "exact"
    assert card.headers["tags"] == "home, lab"
    assert card.headers["refs"] == "memory project_x"
    assert card.details == "Some details."
    assert codec.render(card) == CARD


def test_codec_tags_without_the_header_is_unchanged() -> None:
    desc = "LANE: want\nNEXT STEP: x\n\nd"
    kind, card = codec.classify(desc)
    assert kind == "exact"
    assert "tags" not in card.headers
    assert codec.render(card) == desc


@pytest.mark.parametrize(
    ("value", "home", "expect"),
    [
        ("home, lab", "projects", ["home", "lab"]),
        ("PROJECTS; Home / automotive", "personal", ["projects", "home", "automotive"]),
        ("projects", "projects", []),  # never its own home branch
        ("work, Work, nonsense", "projects", ["work"]),
        ("", "projects", []),
        (None, "projects", []),
    ],
)
def test_parse_tags(value, home, expect) -> None:
    assert codec.parse_tags(value, home) == expect


def _store(path: Path) -> Store:
    s = Store(path, TZ, now=lambda: dt.datetime(2026, 10, 7, 12, 0, tzinfo=dt.UTC))
    s.open()
    return s


def test_store_fills_item_branches_on_every_write(tmp_path: Path) -> None:
    s = _store(tmp_path / "mc.db")
    uid = s.create("projects", "Tagged card", "needs_action", None, CARD, None, None)
    assert s.branches_of(uid) == ["home", "lab"]
    row = s.get(uid)
    assert row["tags"] == "home, lab"
    assert s.describe(row) == CARD  # header text kept as written, in order

    # WS column edit rewrites the index
    s.update_fields(uid, {"tags": "studio"})
    assert s.branches_of(uid) == ["studio"]
    assert "TAGS: studio\nREFS:" in s.describe(s.get(uid))

    # a description edit from the to-do UI parses TAGS back
    desc = s.describe(s.get(uid)).replace("TAGS: studio", "TAGS: automotive, personal")
    assert s.update_from_todo(uid, None, None, None, desc)
    assert s.branches_of(uid) == ["automotive", "personal"]

    # clearing the header empties the index
    s.update_fields(uid, {"tags": ""})
    assert s.branches_of(uid) == []
    assert s.get(uid)["tags"] is None
    assert "TAGS" not in s.describe(s.get(uid))

    # moving a card to a branch it was tagged with drops that tag from the index
    s.update_fields(uid, {"tags": "home, lab"})
    s.update_fields(uid, {"branch": "lab"})
    assert s.branches_of(uid) == ["home"]
    s.close()


def test_store_adds_the_tags_column_to_an_existing_database(tmp_path: Path) -> None:
    """A database created before TAGS existed must open and gain the column."""
    path = tmp_path / "old.db"
    old_schema = SCHEMA.replace(" tags TEXT,", "")
    assert "tags" not in old_schema
    conn = sqlite3.connect(path)
    conn.executescript(old_schema)
    conn.execute(
        "INSERT INTO items(uid, branch, position, title, status, format, details, "
        "created_at, updated_at) VALUES ('11111111-0000-1000-8000-000000000001', "
        "'projects', 0, 'Old card', 'needs_action', 'lean', 'd', '2026-10-01T00:00:00+00:00', "
        "'2026-10-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()
    s = _store(path)
    cols = {r["name"] for r in s.conn.execute("PRAGMA table_info(items)")}
    assert set(ITEM_COLS) <= cols
    row = s.get("11111111-0000-1000-8000-000000000001")
    assert row["tags"] is None and row["title"] == "Old card"
    s.update_fields(row["uid"], {"tags": "home"})
    assert s.branches_of(row["uid"]) == ["home"]
    s.close()


def test_export_carries_tags(tmp_path: Path) -> None:
    s = _store(tmp_path / "mc.db")
    uid = s.create("projects", "Tagged", "needs_action", None, CARD, None, None)
    exp = s.export()
    item = next(i for i in exp["items"] if i["uid"] == uid)
    assert item["tags"] == ["home", "lab"]
    assert exp["todo_lists"]["projects"][0]["description"] == CARD
    assert "home" not in exp["todo_lists"]  # projected into the home branch only
    s.close()
