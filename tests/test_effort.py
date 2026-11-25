"""EFFORT / COST headers (2026-10-11) — codec and store, no Home Assistant needed."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "custom_components" / "mission_control"
if "mission_control" not in sys.modules:
    import types

    pkg = types.ModuleType("mission_control")
    pkg.__path__ = [str(PKG)]
    sys.modules["mission_control"] = pkg

from mission_control import codec  # noqa: E402
from mission_control.store import ITEM_COLS, SCHEMA  # noqa: E402
from tests.test_tags import _store  # noqa: E402

CARD = (
    "LANE: need\n"
    "TOOLBOX: Attic\n"
    "BUY FIRST: Online order\n"
    "EFFORT: 2h\n"
    "COST: $45\n"
    "NEXT STEP: Do the thing.\n"
    "\n"
    "Some details.\n"
    "\n"
    "PROGRESS:\n"
    "2026-10-11 — created"
)


def test_codec_round_trips_effort_and_cost() -> None:
    kind, card = codec.classify(CARD)
    assert kind == "exact"
    assert card.headers["effort"] == "2h" and card.headers["cost"] == "$45"
    assert codec.render(card) == CARD


def test_codec_puts_effort_and_cost_after_buy_first() -> None:
    c = codec.Card(headers={"next_step": "Go.", "cost": "$10", "lane": "want", "effort": "30m"})
    assert codec.render(c).split("\n")[:4] == ["LANE: want", "EFFORT: 30m", "COST: $10", "NEXT STEP: Go."]


def test_store_adds_effort_and_cost_to_an_existing_database(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    old_schema = SCHEMA.replace(" effort TEXT,", "").replace(" cost TEXT,", "")
    assert "effort" not in old_schema and " cost " not in old_schema
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
    uid = "11111111-0000-1000-8000-000000000001"
    s.update_fields(uid, {"effort": "1h", "cost": "$20"})
    row = s.get(uid)
    assert row["effort"] == "1h" and row["cost"] == "$20"
    s.update_fields(uid, {"cost": ""})
    assert s.get(uid)["cost"] is None
    s.close()
