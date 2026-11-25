"""Package orders (card: package tracker, 2026-10-26).

One row per order, fed by the email parser (IMAP -> ai_task -> service
mission_control.order_upsert).  Synchronous, no Home Assistant imports; every
call runs inside MissionStore._tx from the executor, like the rest of the store.

Order blocks: a card with ``BLOCKED BY: order 111-6879291-2720237`` keeps that
as a text block (codec.parse_blocks never sees a uid or date in it).  The block
resolver clears a text block whose words are only ``order <number>`` tokens once
every named order is delivered — see ``text_block_orders`` / ``orders_delivered``.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
  key TEXT PRIMARY KEY,
  store TEXT NOT NULL,
  order_number TEXT,
  account TEXT,
  stage TEXT NOT NULL,
  stage_rank INTEGER NOT NULL,
  delayed INTEGER NOT NULL DEFAULT 0,
  expected_date TEXT,
  expected_window TEXT,
  carrier TEXT,
  tracking TEXT,
  items TEXT,
  total TEXT,
  url TEXT,
  card_uid TEXT,
  carrier_seen TEXT,
  carrier_seen_at TEXT,
  carrier_evidence TEXT,
  snapshot TEXT,
  first_seen TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  ordered_at TEXT,
  shipped_at TEXT,
  delivered_at TEXT,
  collected_at TEXT,
  message_ids TEXT NOT NULL DEFAULT '[]',
  events TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS orders_number ON orders(order_number);
CREATE INDEX IF NOT EXISTS orders_tracking ON orders(tracking);
"""

STAGES = {
    "ordered": 1,
    "shipped": 2,
    "out_for_delivery": 3,
    "delivered": 4,
    "cancelled": 5,
}
ACTIVE_STAGES = ("ordered", "shipped", "out_for_delivery")
ORDER_FIELDS = (
    "key", "store", "order_number", "account", "stage", "delayed", "expected_date",
    "expected_window", "carrier", "tracking", "items", "total", "url", "card_uid",
    "carrier_seen", "carrier_seen_at", "carrier_evidence", "snapshot", "first_seen",
    "updated_at", "ordered_at", "shipped_at", "delivered_at", "collected_at",
    "items_locked", "item_list",
)
_ORDER_TOKEN = re.compile(r"\border\s+#?([A-Za-z0-9][A-Za-z0-9-]{3,})", re.I)
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(orders)")}
    if "items_locked" not in cols:
        # 2026-10-27: a name typed on the Packages page survives later emails
        conn.execute("ALTER TABLE orders ADD COLUMN items_locked INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    if "item_list" not in cols:
        # 2026-11-01: every line of a multi-item (grocery) order, "name | qty | price ; ..."
        conn.execute("ALTER TABLE orders ADD COLUMN item_list TEXT")
        conn.commit()


def _norm_store(s: str | None) -> str:
    s = (s or "").strip()
    low = s.lower()
    if "amazon" in low:
        return "Amazon"
    return s or "Unknown"


def _clean(v) -> str | None:
    if v is None:
        return None
    v = str(v).strip()
    return v or None


def make_key(store: str, order_number: str | None, tracking: str | None) -> str | None:
    ident = order_number or tracking
    if not ident:
        return None
    return f"{_norm_store(store).lower()}:{ident.strip().lower()}"


def _row(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    d = {k: r[k] for k in ORDER_FIELDS}
    d["delayed"] = bool(d["delayed"])
    d["items_locked"] = bool(d["items_locked"])
    d["events"] = json.loads(r["events"] or "[]")
    return d


def find(conn: sqlite3.Connection, ref: str) -> dict | None:
    """By key, order number or tracking number."""
    ref = (ref or "").strip()
    if not ref:
        return None
    r = conn.execute(
        "SELECT * FROM orders WHERE key = ? OR order_number = ? OR tracking = ? "
        "ORDER BY updated_at DESC LIMIT 1",
        (ref.lower(), ref, ref),
    ).fetchone()
    return _row(r)


def upsert(conn: sqlite3.Connection, now_iso: str, data: dict) -> dict:
    """Merge one parsed email into its order.  Returns {order, changed, stage_changed, prev_stage}.

    Stage only moves forward (an old 'shipped' email replayed after 'delivered'
    does not regress it); 'delayed' sets a flag without changing the stage;
    'cancelled' always wins.  A message_id seen before is a no-op.
    """
    store = _norm_store(data.get("store"))
    order_number = _clean(data.get("order_number"))
    tracking = _clean(data.get("tracking_number") or data.get("tracking"))
    key = make_key(store, order_number, tracking)
    if key is None:
        raise ValueError("order_number or tracking_number is required")
    stage = (_clean(data.get("stage")) or "").lower().replace(" ", "_")
    delayed = stage == "delayed"
    if delayed or stage not in STAGES:
        stage = None
    msg_id = _clean(data.get("message_id"))
    received = _clean(data.get("received")) or now_iso

    cur = conn.execute("SELECT * FROM orders WHERE key = ?", (key,)).fetchone()
    if cur is None:
        # 2026-11-01: one order reaches us under several store names — the
        # retailer's confirmation, its pharmacy portal, the carrier's shipment
        # notice.  Match on the identifiers
        # themselves, whatever the email called the store.
        if order_number and len(order_number) >= 6:
            cur = conn.execute(
                "SELECT * FROM orders WHERE order_number = ? ORDER BY updated_at DESC LIMIT 1",
                (order_number,),
            ).fetchone()
        if cur is None and tracking:
            cur = conn.execute(
                "SELECT * FROM orders WHERE tracking = ? "
                "ORDER BY (order_number IS NOT NULL) DESC, updated_at DESC LIMIT 1",
                (tracking,),
            ).fetchone()
    old = _row(cur)
    if cur is not None and msg_id and msg_id in json.loads(cur["message_ids"] or "[]"):
        return {"order": old, "changed": False, "stage_changed": False, "prev_stage": old["stage"]}

    new = dict(old) if old else {
        "key": key, "store": store, "order_number": order_number, "account": None,
        "stage": stage or "ordered", "delayed": False, "first_seen": now_iso,
        "card_uid": None, "carrier_seen": None, "carrier_seen_at": None,
        "carrier_evidence": None, "snapshot": None, "items_locked": False, "ordered_at": None,
        "shipped_at": None, "delivered_at": None, "collected_at": None, "events": [],
    }
    if old and old["key"] != key and order_number and not old.get("order_number"):
        # promote a tracking-keyed row to its order number (a row that already
        # has a number keeps its key even when this email names the store differently)
        _rekey(conn, old["key"], key)
        new["key"] = key
    new["order_number"] = new.get("order_number") or order_number

    for f, src in (
        ("account", "account"), ("expected_window", "expected_window"),
        ("carrier", "carrier"), ("items", "items"), ("total", "total"), ("url", "order_url"),
    ):
        if f == "items" and new.get("items_locked"):
            continue  # renamed by hand on the Packages page
        v = _clean(data.get(src))
        if v:
            new[f] = v
    ed = _clean(data.get("expected_date"))
    if ed and _DATE.match(ed):
        new["expected_date"] = ed
    il = _clean(data.get("item_list"))
    if il and (";" in il or "|" in il or not new.get("item_list")):
        new["item_list"] = il[:6000]
    if tracking:
        new["tracking"] = tracking
    if store != "Unknown" and (old is None or (old.get("store") or "Unknown") == "Unknown"):
        new["store"] = store  # the first real store name sticks

    prev_stage = old["stage"] if old else None
    if stage:
        cur_rank = STAGES.get(new["stage"], 0) if old else 0
        if stage == "cancelled" or STAGES[stage] > cur_rank or old is None:
            new["stage"] = stage
            if stage != "delivered":
                new["delayed"] = False
    if delayed:
        new["delayed"] = True
    stamp_col = {"ordered": "ordered_at", "shipped": "shipped_at",
                 "out_for_delivery": "shipped_at", "delivered": "delivered_at"}.get(new["stage"])
    if stamp_col and not new.get(stamp_col):
        new[stamp_col] = received
    stage_changed = new["stage"] != prev_stage

    ids = json.loads(cur["message_ids"]) if cur is not None else []
    if msg_id:
        ids.append(msg_id)
    events = list(new.get("events") or [])
    events.append({"ts": received, "stage": stage or ("delayed" if delayed else None),
                   "subject": _clean(data.get("subject"))})
    new["events"] = events[-30:]
    new["updated_at"] = now_iso
    _write(conn, new, ids)
    return {"order": find(conn, new["key"]), "changed": True,
            "stage_changed": stage_changed, "prev_stage": prev_stage}


def _rekey(conn: sqlite3.Connection, old_key: str, new_key: str) -> None:
    """Move an order to a new key and carry every reference to it along."""
    conn.execute("DELETE FROM orders WHERE key = ?", (old_key,))
    for table in ("shop_items", "shop_questions"):
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (table,)).fetchone():
            conn.execute(f"UPDATE {table} SET order_key = ? WHERE order_key = ?",  # noqa: S608
                         (new_key, old_key))


def merge(conn: sqlite3.Connection, now_iso: str, into_ref: str, from_ref: str) -> dict:
    """Fold order ``from_ref`` into ``into_ref`` (duplicates from before the
    identifier matching above).  Gaps fill from the duplicate, the stage is the
    furthest of the two, message ids and events are unioned, and shopping-list
    references move over."""
    a, b = find(conn, into_ref), find(conn, from_ref)
    if a is None or b is None:
        raise ValueError("both orders must exist")
    if a["key"] == b["key"]:
        raise ValueError("that is the same order")
    ra = conn.execute("SELECT message_ids FROM orders WHERE key = ?", (a["key"],)).fetchone()
    rb = conn.execute("SELECT message_ids FROM orders WHERE key = ?", (b["key"],)).fetchone()
    new = dict(a)
    for f in ("order_number", "account", "expected_date", "expected_window", "carrier", "tracking",
              "items", "total", "url", "card_uid", "carrier_seen", "carrier_seen_at",
              "carrier_evidence", "snapshot", "ordered_at", "shipped_at", "delivered_at",
              "collected_at", "item_list"):
        if not new.get(f) and b.get(f):
            new[f] = b[f]
    if b["stage"] == "cancelled" or STAGES.get(b["stage"], 0) > STAGES.get(new["stage"], 0):
        new["stage"] = b["stage"]
    new["delayed"] = bool(a["delayed"] or b["delayed"])
    new["items_locked"] = bool(a["items_locked"] or b["items_locked"])
    new["first_seen"] = min(a["first_seen"], b["first_seen"])
    new["events"] = sorted(list(a["events"]) + list(b["events"]),
                           key=lambda e: e.get("ts") or "")[-30:]
    new["updated_at"] = now_iso
    ids = json.loads(ra["message_ids"] or "[]") + json.loads(rb["message_ids"] or "[]")
    _rekey(conn, b["key"], a["key"])
    _write(conn, new, ids)
    return find(conn, a["key"])


def _write(conn: sqlite3.Connection, o: dict, ids: list[str]) -> None:
    cols = [f for f in ORDER_FIELDS] + ["stage_rank", "message_ids", "events"]
    vals = [o.get(f) for f in ORDER_FIELDS]
    vals[ORDER_FIELDS.index("delayed")] = 1 if o.get("delayed") else 0
    vals[ORDER_FIELDS.index("items_locked")] = 1 if o.get("items_locked") else 0
    vals += [STAGES.get(o["stage"], 0), json.dumps(ids[-50:]), json.dumps(o.get("events") or [])]
    conn.execute(
        f"INSERT OR REPLACE INTO orders({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
        vals,
    )


def update_fields(conn: sqlite3.Connection, now_iso: str, ref: str, fields: dict) -> dict:
    """Set card_uid / carrier_seen(+evidence, snapshot) / collected_at on one order."""
    o = find(conn, ref)
    if o is None:
        raise ValueError(f"no order matches {ref!r}")
    allowed = {"card_uid", "carrier_seen", "carrier_evidence", "snapshot", "collected_at", "items"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if "carrier_seen" in sets:
        sets["carrier_seen_at"] = now_iso
    if "items" in sets:
        # a hand-typed name is locked against later emails; clearing it unlocks
        sets["items"] = _clean(sets["items"])
        sets["items_locked"] = 1 if sets["items"] else 0
    if not sets:
        return o
    sets["updated_at"] = now_iso
    conn.execute(
        f"UPDATE orders SET {', '.join(f'{k} = ?' for k in sets)} WHERE key = ?",
        [*sets.values(), o["key"]],
    )
    return find(conn, o["key"])


STALE_DAYS = 21
OVERDUE_GRACE_DAYS = 3  # past-due by more than this -> stale (no delivered email ever came)


def _local_date(ts: str | None, tz) -> str:
    if not ts:
        return ""
    try:
        d = dt.datetime.fromisoformat(ts.replace(" ", "T"))
    except ValueError:
        return ts[:10]
    if d.tzinfo is None or tz is None:
        return d.date().isoformat()
    return d.astimezone(tz).date().isoformat()


def listing(conn: sqlite3.Connection, today: str, recent_days: int = 3, tz=None) -> dict:
    """Everything the sensors and dashboard need, bucketed (dates in house time).

    An open order with no email for STALE_DAYS goes to 'stale' (not counted):
    many stores never send a delivered email.
    """
    t = dt.date.fromisoformat(today)
    since = (t - dt.timedelta(days=recent_days + 1)).isoformat()
    stale_before = (t - dt.timedelta(days=STALE_DAYS)).isoformat()
    rows = [_row(r) for r in conn.execute(
        "SELECT * FROM orders WHERE stage IN ('ordered','shipped','out_for_delivery') "
        "OR (stage = 'delivered' AND substr(delivered_at,1,10) >= ?) "
        "ORDER BY COALESCE(expected_date,'9999'), updated_at", (since,))]
    out = {"arriving_today": [], "out_for_delivery": [], "on_the_way": [], "not_shipped": [],
           "delivered_today": [], "delivered_recent": [], "overdue": [], "stale": []}
    for o in rows:
        st, ed = o["stage"], o.get("expected_date")
        if st == "delivered":
            dd = _local_date(o.get("delivered_at"), tz)
            if dd == today:
                out["delivered_today"].append(o)
            elif dd >= (t - dt.timedelta(days=recent_days)).isoformat():
                out["delivered_recent"].append(o)
            continue
        last = max([_local_date(e.get("ts"), tz) for e in o.get("events") or []] or [""])
        overdue_cut = (t - dt.timedelta(days=OVERDUE_GRACE_DAYS)).isoformat()
        if (last and last < stale_before and not (ed and ed >= today)) or (ed and ed < overdue_cut):
            out["stale"].append(o)
            continue
        if st == "out_for_delivery":
            out["out_for_delivery"].append(o)
        if ed == today or st == "out_for_delivery":
            out["arriving_today"].append(o)
        elif ed and ed < today:
            out["overdue"].append(o)
        if st == "ordered":
            out["not_shipped"].append(o)
        elif st == "shipped":
            out["on_the_way"].append(o)
    return out


def text_block_orders(value: str) -> list[str] | None:
    """Order numbers named by a text block, or None if it says anything else."""
    toks = _ORDER_TOKEN.findall(value or "")
    if not toks:
        return None
    rest = _ORDER_TOKEN.sub(" ", value)
    if re.search(r"[A-Za-z0-9]", re.sub(r"\b(and|or|the|orders?|&|,|;)\b", " ", rest, flags=re.I)):
        return None
    return toks


def orders_delivered(conn: sqlite3.Connection, numbers: list[str]) -> bool:
    for n in numbers:
        o = find(conn, n)
        if o is None or o["stage"] not in ("delivered", "cancelled"):
            return False
    return True
