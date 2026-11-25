"""SQLite store for Mission Control.

Synchronous and free of Home Assistant imports so it can be tested on its own.
The integration calls it from the executor, one call at a time, under an
asyncio lock (see __init__.py) — every write is serialized.

The database is the single source of truth.  The eight to-do lists are a
projection: each card's description is rendered from its columns on read
(codec.render), and an edited description is parsed back into columns
(codec.parse) on write.  Nothing about a card is stored twice; ``links`` with
origin 'card' is a derived index, rebuilt from PARENT / BLOCKED BY on every
write.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable
import datetime as dt
import difflib
import json
from pathlib import Path
import re
import sqlite3
import uuid

from . import orders as orders_mod
from . import shopping as shop_mod
from .codec import COLUMNS, Card, classify, next_due, parse_blocks, parse_repeat, parse_tags, render

SCHEMA_VERSION = 1
_PARTS_BLOCK = re.compile(r"^\s*(parts|parts to buy|shopping|shopping list|the parts)\s*\.?\s*$", re.I)
HISTORY_KEEP_DAYS = 180
# Fields whose changes are recorded in item_history.
HISTORY_FIELDS: tuple[str, ...] = ("title", "status", "due", "branch", "details", *COLUMNS)

ITEM_COLS: tuple[str, ...] = (
    "uid",
    "branch",
    "position",
    "title",
    "status",
    "due",
    "format",
    *COLUMNS,
    "details",
    "created_at",
    "updated_at",
    "completed_at",
)

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS items (
  uid TEXT PRIMARY KEY,
  branch TEXT NOT NULL,
  position INTEGER NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'needs_action',
  due TEXT,
  format TEXT NOT NULL DEFAULT 'lean',
  {", ".join(c + " TEXT" for c in COLUMNS)},
  details TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT
);
CREATE INDEX IF NOT EXISTS items_branch ON items(branch, position);
CREATE TABLE IF NOT EXISTS notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_uid TEXT NOT NULL REFERENCES items(uid) ON DELETE CASCADE,
  seq INTEGER NOT NULL,
  ts TEXT NOT NULL,
  text TEXT NOT NULL,
  added_at TEXT
);
CREATE INDEX IF NOT EXISTS notes_item ON notes(item_uid, seq);
CREATE TABLE IF NOT EXISTS links (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  from_uid TEXT NOT NULL REFERENCES items(uid) ON DELETE CASCADE,
  to_uid TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('parent', 'blocked_by', 'duplicate')),
  origin TEXT NOT NULL DEFAULT 'card' CHECK (origin IN ('card', 'api')),
  created_at TEXT NOT NULL,
  UNIQUE (from_uid, to_uid, kind)
);
CREATE TABLE IF NOT EXISTS item_branches (
  item_uid TEXT NOT NULL REFERENCES items(uid) ON DELETE CASCADE,
  branch TEXT NOT NULL,
  PRIMARY KEY (item_uid, branch)
);
CREATE TABLE IF NOT EXISTS blocks (
  item_uid TEXT NOT NULL REFERENCES items(uid) ON DELETE CASCADE,
  seq INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('uid', 'date', 'text')),
  value TEXT NOT NULL,
  target_uid TEXT,
  cleared_at TEXT,
  cleared_by TEXT,
  PRIMARY KEY (item_uid, seq)
);
CREATE TABLE IF NOT EXISTS calendar_events (
  item_uid TEXT PRIMARY KEY,
  event_uid TEXT,
  due TEXT NOT NULL,
  title TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS item_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_uid TEXT NOT NULL,
  ts TEXT NOT NULL,
  source TEXT NOT NULL,
  field TEXT NOT NULL,
  old TEXT,
  new TEXT
);
CREATE INDEX IF NOT EXISTS item_history_item ON item_history(item_uid, id);
CREATE TABLE IF NOT EXISTS deleted_items (
  uid TEXT NOT NULL,
  branch TEXT NOT NULL,
  payload TEXT NOT NULL,
  deleted_at TEXT NOT NULL
);
"""

_UIDISH = re.compile(
    r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})?\b"
)
_UUID1_EPOCH = 0x01B21DD213814000


class StoreError(Exception):
    """Raised for a rejected store operation."""


def uuid1_created(uid: str) -> dt.datetime | None:
    """Creation time encoded in a UUIDv1 uid (what the panel shows as Created)."""
    try:
        u = uuid.UUID(uid)
    except (ValueError, TypeError):
        return None
    if u.version != 1:
        return None
    return dt.datetime.fromtimestamp((u.time - _UUID1_EPOCH) / 1e7, dt.UTC)


class MissionStore:
    """All reads and writes of the Mission Control database."""

    def __init__(
        self,
        path: str | Path,
        tz: dt.tzinfo,
        now: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self.path = str(path)
        self.tz = tz
        self._now = now or (lambda: dt.datetime.now(dt.UTC))
        self.conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------------ setup
    def open(self) -> None:
        conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(SCHEMA)
        orders_mod.ensure_schema(conn)  # package tracker (2026-10-26)
        shop_mod.ensure_schema(conn)  # shopping list (2026-10-29)
        # Header columns added after the first release (TAGS, 2026-10-07):
        # CREATE TABLE IF NOT EXISTS leaves an existing table alone, so add
        # any column the codec knows that the table does not have yet.
        have = {r["name"] for r in conn.execute("PRAGMA table_info(items)")}
        for col in COLUMNS:
            if col not in have:
                conn.execute(f"ALTER TABLE items ADD COLUMN {col} TEXT")
        conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self.conn = conn
        # Field history (2026-10-08): keep 180 days.
        cutoff = (self._now() - dt.timedelta(days=HISTORY_KEEP_DAYS)).isoformat(timespec="seconds")
        conn.execute("DELETE FROM item_history WHERE ts < ?", (cutoff,))
        # Blocks index (2026-10-08): cards written before the
        # table existed have no rows yet — index every open BLOCKED BY once.
        if conn.execute("SELECT COUNT(*) FROM blocks").fetchone()[0] == 0:
            rows = conn.execute(
                "SELECT * FROM items WHERE status = 'needs_action' "
                "AND blocked_by IS NOT NULL AND blocked_by != ''"
            ).fetchall()
            if rows:
                conn.execute("BEGIN IMMEDIATE")
                for r in rows:
                    self._reindex_blocks(dict(r), {})
                conn.execute("COMMIT")

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def _now_iso(self) -> str:
        return self._now().isoformat(timespec="seconds")

    def _today(self) -> str:
        return self._now().astimezone(self.tz).date().isoformat()

    # ------------------------------------------------------------------ reads
    def _notes(self, uids: list[str]) -> dict[str, list[sqlite3.Row]]:
        out: dict[str, list[sqlite3.Row]] = {u: [] for u in uids}
        if not uids:
            return out
        for chunk in range(0, len(uids), 500):
            part = uids[chunk : chunk + 500]
            q = ",".join("?" * len(part))
            for r in self.conn.execute(
                f"SELECT * FROM notes WHERE item_uid IN ({q}) ORDER BY item_uid, seq",
                part,
            ):
                out[r["item_uid"]].append(r)
        return out

    def items(
        self, branch: str | None = None, uids: Iterable[str] | None = None
    ) -> list[dict]:
        """Rows (as dicts, with 'notes') ordered by branch then position."""
        sql, args = "SELECT * FROM items", []
        if branch is not None:
            sql, args = sql + " WHERE branch = ?", [branch]
        elif uids is not None:
            uids = list(uids)
            if not uids:
                return []
            sql += f" WHERE uid IN ({','.join('?' * len(uids))})"
            args = uids
        sql += " ORDER BY branch, position"
        rows = [dict(r) for r in self.conn.execute(sql, args)]
        notes = self._notes([r["uid"] for r in rows])
        for r in rows:
            r["notes"] = [(n["ts"], n["text"]) for n in notes[r["uid"]]]
            r["note_added"] = [n["added_at"] for n in notes[r["uid"]]]
        return rows

    def projected(
        self, branch: str, window_days: int = 30, floor: int = 20
    ) -> list[dict]:
        """Rows the to-do list shows (2026-10-08).

        Every open card, plus closed cards completed within ``window_days`` or
        among the ``floor`` most recently closed — whichever is more.  Older
        closed cards stay in the store, the WS API and the export only.
        """
        rows = self.items(branch)
        cutoff = (
            (self._now() - dt.timedelta(days=window_days)).isoformat(timespec="seconds")
        )
        closed = sorted(
            (r for r in rows if r["status"] == "completed"),
            key=lambda r: r.get("completed_at") or "",
            reverse=True,
        )
        keep = {r["uid"] for r in closed[:floor]}
        keep.update(r["uid"] for r in closed if (r.get("completed_at") or "") >= cutoff)
        return [r for r in rows if r["status"] != "completed" or r["uid"] in keep]

    # ------------------------------------------------------- derived lists
    LANE_RANK = {"urgent": 0, "work": 1, "need": 2, "want": 3, None: 4, "": 4}

    def view_rows(self, view: str, owner_of, today: str | None = None) -> list[dict]:
        """Query-backed lists (2026-10-08).

        ``ready_for_you``: open, effective owner "me" (``owner_of`` is the
        shared rule from websocket.owner_of), no live block; lane, then due,
        then branch.  ``this_week``: open cards due within 7 days or overdue,
        soonest first.  ``today`` (docs/CARD_FORMAT.md (Today rank), 2026-11-01): overdue, due
        today and undated urgent cards, unblocked, in that order.  Read-only
        projections — nothing is stored twice.
        """
        today = today or self._today()
        rows = [r for r in self.items() if r["status"] == "needs_action"]
        if view == "ready_for_you":
            out = [
                r for r in rows
                if owner_of(r) == "me" and not self.is_blocked(r["uid"])
            ]
            out.sort(
                key=lambda r: (
                    self.LANE_RANK.get(r.get("lane"), 4),
                    (r.get("due") or "9999")[:10],
                    r["branch"],
                    r["position"],
                )
            )
            return out
        if view == "this_week":
            limit = (dt.date.fromisoformat(today) + dt.timedelta(days=7)).isoformat()
            out = [r for r in rows if r.get("due") and r["due"][:10] <= limit]
            out.sort(key=lambda r: (r["due"][:10], r["branch"], r["position"]))
            return out
        if view == "today":
            # Today rank (docs/CARD_FORMAT.md (Today rank), 2026-11-01): 0 overdue (oldest first) → 1 due today →
            # 2 urgent lane with no due.  Live blocks stay out; tomorrow / this week stay on
            # this_week.  Each row carries view_label for the list's description.
            labels = ("Overdue", "Due today", "Urgent")
            out: list[tuple[int, dict]] = []
            for r in rows:
                d = (r.get("due") or "")[:10]
                if d and d < today:
                    tier = 0
                elif d and d == today:
                    tier = 1
                elif not d and r.get("lane") == "urgent":
                    tier = 2
                else:
                    continue
                if self.is_blocked(r["uid"]):
                    continue
                late = (dt.date.fromisoformat(today) - dt.date.fromisoformat(d)).days if tier == 0 else 0
                r["view_label"] = f"Overdue {late}d" if tier == 0 else labels[tier]
                out.append((tier, r))
            out.sort(
                key=lambda x: (
                    x[0],
                    (x[1].get("due") or "")[:10],
                    self.LANE_RANK.get(x[1].get("lane"), 4),
                    x[1]["branch"],
                    x[1]["position"],
                )
            )
            return [r for _, r in out]
        raise StoreError(f"unknown view {view}")

    def get(self, uid: str) -> dict | None:
        rows = self.items(uids=[uid])
        return rows[0] if rows else None

    def resolve(self, ref: str) -> str | None:
        """Full uid for a uid or a unique uid prefix (8+ chars)."""
        ref = (ref or "").strip().lower()
        if len(ref) < 8:
            return None
        rows = self.conn.execute(
            "SELECT uid FROM items WHERE uid LIKE ? LIMIT 2", (ref + "%",)
        ).fetchall()
        return rows[0]["uid"] if len(rows) == 1 else None

    def links(self, uid: str) -> list[dict]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT from_uid, to_uid, kind, origin FROM links "
                "WHERE from_uid = ? OR to_uid = ? ORDER BY id",
                (uid, uid),
            )
        ]

    def branches_of(self, uid: str) -> list[str]:
        return [
            r["branch"]
            for r in self.conn.execute(
                "SELECT branch FROM item_branches WHERE item_uid = ? ORDER BY branch",
                (uid,),
            )
        ]

    @staticmethod
    def card_of(row: dict) -> Card:
        return Card(
            headers={c: row[c] for c in COLUMNS if row.get(c) is not None},
            details=row.get("details") or "",
            notes=list(row.get("notes") or []),
        )

    @classmethod
    def describe(cls, row: dict) -> str | None:
        """The projected description of a row."""
        if row["format"] == "raw":
            return row["details"] if row["details"] != "" else None
        return render(cls.card_of(row))

    def count_open(self, branch: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM items WHERE branch = ? AND status = 'needs_action'",
            (branch,),
        ).fetchone()[0]

    # ---------------------------------------------------------- write helpers
    def _begin(self) -> None:
        self.conn.execute("BEGIN IMMEDIATE")

    def _commit(self) -> None:
        self.conn.execute("COMMIT")

    def _rollback(self) -> None:
        if self.conn.in_transaction:
            self.conn.execute("ROLLBACK")

    def _tx(self, fn, *args, **kw):
        self._begin()
        try:
            out = fn(*args, **kw)
        except BaseException:
            self._rollback()
            raise
        self._commit()
        return out

    @staticmethod
    def _apply_description(row: dict, desc: str | None) -> list[tuple[str, str]]:
        """Parse ``desc`` into ``row`` (in place); return the parsed notes."""
        kind, card = classify(desc)
        for c in COLUMNS:
            row[c] = None
        if kind == "raw":
            row["format"] = "raw"
            row["details"] = desc or ""
            return []
        row["format"] = "lean"
        row.update(card.headers)
        row["details"] = card.details
        return card.notes

    def _write_row(self, row: dict) -> None:
        cols = [c for c in ITEM_COLS]
        self.conn.execute(
            f"INSERT OR REPLACE INTO items ({','.join(cols)}) "
            f"VALUES ({','.join('?' * len(cols))})",
            [row.get(c) for c in cols],
        )

    def _write_notes(
        self,
        uid: str,
        notes: list[tuple[str, str]],
        old: list[tuple[str, str]],
        old_added: list[str | None],
        stamp_new: bool,
    ) -> None:
        """Replace an item's notes, keeping added_at for notes that survive."""
        pool: dict[tuple[str, str], list[str | None]] = {}
        for n, a in zip(old, old_added, strict=False):
            pool.setdefault(n, []).append(a)
        self.conn.execute("DELETE FROM notes WHERE item_uid = ?", (uid,))
        now = self._now_iso() if stamp_new else None
        for seq, n in enumerate(notes):
            added = pool[n].pop(0) if pool.get(n) else now
            self.conn.execute(
                "INSERT INTO notes(item_uid, seq, ts, text, added_at) VALUES (?,?,?,?,?)",
                (uid, seq, n[0], n[1], added),
            )

    def _reindex_links(self, row: dict) -> None:
        uid = row["uid"]
        self.conn.execute(
            "DELETE FROM links WHERE from_uid = ? AND origin = 'card'", (uid,)
        )
        targets: list[tuple[str, str]] = []
        if row.get("parent_uid"):
            to = self.resolve(row["parent_uid"])
            if to:
                targets.append((to, "parent"))
        for tok in _UIDISH.findall(row.get("blocked_by") or ""):
            to = self.resolve(tok)
            if to and to != uid:
                targets.append((to, "blocked_by"))
        for to, kind in targets:
            self.conn.execute(
                "INSERT OR IGNORE INTO links(from_uid, to_uid, kind, origin, created_at) "
                "VALUES (?,?,?,'card',?)",
                (uid, to, kind, self._now_iso()),
            )

    def _reindex_tags(self, row: dict) -> None:
        """Rebuild item_branches from the card's TAGS header.

        ``items.branch`` stays the home branch; item_branches holds the OTHER
        branches the card belongs to.  The header text itself is kept as
        written in the ``tags`` column, so an unknown word is not lost.
        """
        uid = row["uid"]
        self.conn.execute("DELETE FROM item_branches WHERE item_uid = ?", (uid,))
        for b in parse_tags(row.get("tags"), row.get("branch")):
            self.conn.execute(
                "INSERT OR IGNORE INTO item_branches(item_uid, branch) VALUES (?, ?)",
                (uid, b),
            )

    # ------------------------------------------------------------ history
    def _history(self, uid: str, source: str, field: str, old, new) -> None:
        self.conn.execute(
            "INSERT INTO item_history(item_uid, ts, source, field, old, new) VALUES (?,?,?,?,?,?)",
            (uid, self._now_iso(), source, field, old, new),
        )

    def _history_diff(self, uid: str, old: dict, new: dict, source: str) -> None:
        """One history row per changed field."""
        for f in HISTORY_FIELDS:
            a, b = old.get(f), new.get(f)
            if (a or None) != (b or None):
                self._history(uid, source, f, a, b)

    def history(self, uid: str, limit: int = 50) -> list[dict]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT id, ts, source, field, old, new FROM item_history "
                "WHERE item_uid = ? ORDER BY id DESC LIMIT ?",
                (uid, limit),
            )
        ]

    def changed_since(self, since_iso: str, branch: str | None = None) -> list[dict]:
        """Items with any recorded change after ``since_iso`` (branch reviews)."""
        sql = (
            "SELECT h.item_uid AS uid, i.branch, i.title, i.status, COUNT(*) AS changes, "
            "MAX(h.ts) AS last_change FROM item_history h JOIN items i ON i.uid = h.item_uid "
            "WHERE h.ts > ?"
        )
        args: list = [since_iso]
        if branch:
            sql += " AND i.branch = ?"
            args.append(branch)
        sql += " GROUP BY h.item_uid ORDER BY last_change DESC"
        return [dict(r) for r in self.conn.execute(sql, args)]

    def activity_today(self) -> dict:
        """What moved today (the two-push digest, 2026-11-02), read from item_history since local
        midnight: ``closed`` = status -> completed and not reopened later; ``created`` = created
        rows; ``freed`` = the resolver's unblocked notes (source system:unblock); ``moved`` = any
        other change or note by a person (todo / ws / panel / revert) on a card that was neither
        closed nor created today.  Deleted cards drop out (no items row to join)."""
        now = self._now()
        local = now.astimezone(self.tz)
        midnight = dt.datetime.combine(local.date(), dt.time(0), self.tz).astimezone(now.tzinfo)
        since = midnight.isoformat(timespec="seconds")
        rows = self.conn.execute(
            "SELECT h.item_uid AS uid, h.ts, h.source, h.field, h.old, h.new, i.title, i.branch "
            "FROM item_history h JOIN items i ON i.uid = h.item_uid WHERE h.ts >= ? ORDER BY h.id",
            (since,),
        ).fetchall()
        closed: dict[str, dict] = {}
        moved: dict[str, dict] = {}
        freed: dict[str, dict] = {}
        created: dict[str, dict] = {}
        for r in rows:
            uid = r["uid"]
            ent = {"uid": uid, "title": r["title"], "branch": r["branch"]}
            src = r["source"] or ""
            if r["field"] == "status":
                if r["new"] == "completed":
                    closed[uid] = ent
                elif r["old"] == "completed":
                    closed.pop(uid, None)
                    moved[uid] = ent
            elif r["field"] == "created":
                created[uid] = ent
            elif src == "system:unblock":
                freed[uid] = ent
            elif src.startswith("system:"):
                continue
            else:
                moved[uid] = ent
        for uid in list(moved):
            if uid in closed or uid in created:
                moved.pop(uid)
        return {
            "day": local.date().isoformat(),
            "since": since,
            "closed": list(closed.values()),
            "moved": list(moved.values()),
            "freed": list(freed.values()),
            "created": list(created.values()),
        }

    def revert(self, history_id: int, source: str = "revert") -> tuple[str, str, str]:
        """Write a history row's old value back (as a new change). Returns (uid, old_branch, new_branch)."""
        return self._tx(self._revert, history_id, source)

    def _revert(self, history_id: int, source: str) -> tuple[str, str, str]:
        h = self.conn.execute(
            "SELECT item_uid, field, old FROM item_history WHERE id = ?", (history_id,)
        ).fetchone()
        if h is None:
            raise StoreError("no such history row")
        if h["field"] not in HISTORY_FIELDS:
            raise StoreError(f"{h['field']} cannot be reverted")
        value = h["old"] if h["old"] is not None else ""
        old_b, new_b = self._update_fields(h["item_uid"], {h["field"]: value}, source)
        return h["item_uid"], old_b, new_b

    def _apply_repeat(self, row: dict, old: dict, notes: list) -> bool:
        """Closing a repeating card reopens it with the next due.

        Called with the row about to be written when its status flips to
        completed.  Mutates ``row`` (status back to needs_action, new due) and
        appends the "done; next due …" note.  A card with REPEAT cleared first
        closes for good like any other.
        """
        rep = parse_repeat(row.get("repeat"))
        if rep is None or row.get("format") == "raw":
            return False
        today = dt.date.fromisoformat(self._today())
        nxt = next_due(rep, old.get("due"), today)
        row["status"] = "needs_action"
        row["completed_at"] = None
        row["due"] = nxt
        notes.append((today.isoformat(), f"done; next due {nxt}"))
        return True

    def _blocks_state(self, uid: str) -> dict:
        """Cleared state per (kind, value) — captured BEFORE _write_row cascades."""
        return {
            (r["kind"], r["value"]): (r["cleared_at"], r["cleared_by"])
            for r in self.conn.execute(
                "SELECT kind, value, cleared_at, cleared_by FROM blocks WHERE item_uid = ?",
                (uid,),
            )
        }

    def _reindex_blocks(self, row: dict, prev: dict | None = None) -> None:
        """Rebuild the blocks of a card from its BLOCKED BY line.

        Cleared state survives a rewrite as long as the same block (kind + value)
        is still named; a uid block carries the resolved target uid.  ``prev`` is
        the state from _blocks_state taken before the row was rewritten (the
        INSERT OR REPLACE in _write_row cascades the old rows away).
        """
        uid = row["uid"]
        old = prev if prev is not None else self._blocks_state(uid)
        self.conn.execute("DELETE FROM blocks WHERE item_uid = ?", (uid,))
        for seq, b in enumerate(parse_blocks(row.get("blocked_by"), lambda t: self.resolve(t) is not None)):
            target = None
            if b["kind"] == "uid":
                target = self.resolve(b["value"])
                if target == uid:
                    continue
            cleared_at, cleared_by = old.get((b["kind"], b["value"]), (None, None))
            self.conn.execute(
                "INSERT INTO blocks(item_uid, seq, kind, value, target_uid, cleared_at, cleared_by) "
                "VALUES (?,?,?,?,?,?,?)",
                (uid, seq, b["kind"], b["value"], target, cleared_at, cleared_by),
            )

    def blocks_of(self, uid: str) -> list[dict]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT seq, kind, value, target_uid, cleared_at, cleared_by "
                "FROM blocks WHERE item_uid = ? ORDER BY seq",
                (uid,),
            )
        ]

    def is_blocked(self, uid: str) -> bool:
        """Blocked = at least one block not yet cleared."""
        return bool(
            self.conn.execute(
                "SELECT 1 FROM blocks WHERE item_uid = ? AND cleared_at IS NULL LIMIT 1",
                (uid,),
            ).fetchone()
        )

    # ---------------------------------------------------------- auto-unblock
    def resolve_blocks(self) -> list[dict]:
        """Clear satisfied blocks on open cards; free cards with none left.

        A uid block clears when its target card is completed, a date block when
        the date has passed (house time).  Text blocks are never cleared here —
        see clear_block.  A card whose last live block clears gets a dated note
        and its BLOCKED BY line removed.  Returns the freed cards.
        """
        return self._tx(self._resolve_blocks)

    def _resolve_blocks(self) -> list[dict]:
        today = self._today()
        now = self._now_iso()
        rows = self.conn.execute(
            "SELECT b.item_uid, b.seq, b.kind, b.value, b.target_uid, t.status AS tstatus "
            "FROM blocks b JOIN items i ON i.uid = b.item_uid "
            "LEFT JOIN items t ON t.uid = b.target_uid "
            "WHERE b.cleared_at IS NULL AND i.status = 'needs_action'"
        ).fetchall()
        touched: set[str] = set()
        for r in rows:
            reason = None
            if r["kind"] == "uid":
                if r["target_uid"] is None:
                    r2 = self.resolve(r["value"])
                    if r2:
                        self.conn.execute(
                            "UPDATE blocks SET target_uid = ? WHERE item_uid = ? AND seq = ?",
                            (r2, r["item_uid"], r["seq"]),
                        )
                        st = self.conn.execute(
                            "SELECT status FROM items WHERE uid = ?", (r2,)
                        ).fetchone()
                        if st and st["status"] == "completed":
                            reason = f"card {r['value']} closed"
                elif r["tstatus"] == "completed":
                    reason = f"card {r['value']} closed"
            elif r["kind"] == "date" and r["value"] <= today:
                reason = f"{r['value']} passed"
            elif r["kind"] == "text":
                # Package tracker (2026-10-26): "order <number>" blocks clear on delivery.
                nums = orders_mod.text_block_orders(r["value"])
                if nums and orders_mod.orders_delivered(self.conn, nums):
                    reason = "delivered: order " + ", ".join(nums)
                # Shopping list (2026-10-29): "BLOCKED BY: parts" clears once every
                # list item tied to this card is bought or delivered.
                elif _PARTS_BLOCK.match(r["value"] or ""):
                    st = [x["status"] for x in self.conn.execute(
                        "SELECT status FROM shop_items WHERE card_uid = ? AND status != 'dismissed'",
                        (r["item_uid"],))]
                    if st and all(x == "done" for x in st):
                        reason = "parts bought"
            if reason:
                self.conn.execute(
                    "UPDATE blocks SET cleared_at = ?, cleared_by = ? WHERE item_uid = ? AND seq = ?",
                    (now, reason, r["item_uid"], r["seq"]),
                )
                touched.add(r["item_uid"])
        return [f for uid in sorted(touched) if (f := self._free_if_clear(uid))]

    def _free_if_clear(self, uid: str) -> dict | None:
        """If every block on ``uid`` is cleared: note it, drop BLOCKED BY, report."""
        if self.is_blocked(uid):
            return None
        reasons = [
            r["cleared_by"]
            for r in self.conn.execute(
                "SELECT cleared_by FROM blocks WHERE item_uid = ? ORDER BY seq", (uid,)
            )
        ]
        if not reasons:
            return None
        old = self.get(uid)
        if old is None or old["status"] != "needs_action":
            return None
        text = "unblocked: " + "; ".join(x for x in reasons if x)
        notes = list(old["notes"])
        if old["format"] != "raw":
            notes.append((self._today(), text))
        row = {k: old[k] for k in ITEM_COLS}
        row["blocked_by"] = None
        row["updated_at"] = self._now_iso()
        # _write_row is INSERT OR REPLACE: children cascade, so rewrite them all.
        self._write_row(row)
        self._write_notes(uid, notes, old["notes"], old["note_added"], stamp_new=True)
        self._reindex_links(row)
        self._reindex_tags(row)
        self._reindex_blocks(row)  # no BLOCKED BY → no rows; the note is the record
        self._history(uid, "system:unblock", "blocked_by", old.get("blocked_by"), None)
        self._history(uid, "system:unblock", "note", None, f"{self._today()} — {text}")
        return {"uid": uid, "branch": old["branch"], "title": old["title"], "reasons": reasons}

    # ------------------------------------------------------------- orders
    def order_upsert(self, data: dict) -> dict:
        return self._tx(self._order_upsert, data)

    def _order_upsert(self, data: dict) -> dict:
        now = self._now_iso()
        res = orders_mod.upsert(self.conn, now, data)
        res["shopping"] = None
        if res.get("changed") and res.get("order"):
            sh = shop_mod.link_order(self.conn, now, res["order"])
            if sh.get("card_uid"):
                res["order"] = orders_mod.update_fields(
                    self.conn, now, res["order"]["key"], {"card_uid": sh["card_uid"]}
                )
            res["shopping"] = sh
        return res

    def order_update(self, ref: str, fields: dict) -> dict:
        def _do():
            o = orders_mod.update_fields(self.conn, self._now_iso(), ref, fields)
            if fields.get("card_uid"):
                # shopping items tracking this order follow it to the card
                self.conn.execute(
                    "UPDATE shop_items SET card_uid = ? WHERE order_key = ? AND card_uid IS NULL",
                    (o["card_uid"], o["key"]))
            return o
        return self._tx(_do)

    def order_merge(self, into_ref: str, from_ref: str) -> dict:
        return self._tx(orders_mod.merge, self.conn, self._now_iso(), into_ref, from_ref)

    def order_find(self, ref: str) -> dict | None:
        return orders_mod.find(self.conn, ref)

    def order_listing(self, recent_days: int = 3) -> dict:
        return orders_mod.listing(self.conn, self._today(), recent_days, self.tz)

    # ------------------------------------------------------------- shopping
    def _card_ref(self, ref: str | None) -> str | None:
        if not ref:
            return None
        uid = self.resolve(ref)
        if uid is None:
            raise StoreError(f"no unique card for {ref!r}")
        return uid

    def shop_add(self, data: dict) -> dict:
        def _do():
            d = dict(data)
            d["card_uid"] = self._card_ref(d.get("card_uid"))
            return shop_mod.add_item(self.conn, self._now_iso(), d)
        return self._tx(_do)

    def shop_update(self, ref: str, fields: dict) -> dict:
        def _do():
            f = dict(fields)
            if f.get("card_uid"):
                f["card_uid"] = self._card_ref(f["card_uid"])
            return shop_mod.update_item(self.conn, self._now_iso(), ref, f)
        return self._tx(_do)

    def shop_check(self, ref: str, store: str | None = None, price=None, source: str = "check") -> dict:
        def _do():
            it = shop_mod.check_item(self.conn, self._now_iso(), ref, store, price, source)
            if it.get("card_uid"):
                old = self.get(it["card_uid"])
                if old and old["format"] != "raw":
                    s = shop_mod.resolve_store(self.conn, store) if store else None
                    where = f" at {s['name']}" if s else ""
                    self._add_note(it["card_uid"], f"bought {it['name']}{where}", None, "system:shopping")
            return it
        return self._tx(_do)

    def shop_uncheck(self, ref: str) -> dict:
        return self._tx(shop_mod.uncheck_item, self.conn, self._now_iso(), ref)

    def shop_items(self, status: str = "open") -> list[dict]:
        rows = shop_mod.items(self.conn, status, now=self._now_iso())
        self._card_titles(rows)
        return rows

    def shop_get(self, ref: str) -> dict | None:
        return shop_mod.get_item(self.conn, ref)

    def _errand_cards(self, store: dict) -> list[dict]:
        """Open cards whose BUY FIRST this trip satisfies (docs/CARD_FORMAT.md (Toolbox))."""
        out = []
        for r in self.items():
            if r["status"] != "needs_action" or not r.get("buy_first"):
                continue
            bf = r["buy_first"]
            s = shop_mod.resolve_store(self.conn, bf)
            hit = False
            if s is not None and s["key"] == store["key"]:
                hit = True
            elif "online" in bf.lower() and store["kind"] == "online":
                hit = True
            elif (s is not None and store["kind"] == "in_store" and s["kind"] == "in_store"
                  and "hardware" in s["categories"] and "hardware" in store["categories"]):
                hit = True  # a "Home Depot run" can be done at Lowe's
            if hit:
                out.append({"uid": r["uid"], "branch": r["branch"], "title": r["title"],
                            "buy_first": bf, "next_step": r.get("next_step"),
                            "blocked": self.is_blocked(r["uid"])})
        return out

    def shop_run(self, store_ref: str) -> dict:
        s = shop_mod.resolve_store(self.conn, store_ref)
        if s is None:
            raise StoreError(f"unknown store {store_ref!r}")
        res = shop_mod.run(self.conn, self._now_iso(), self._today(), s["key"], self._errand_cards(s))
        self._card_titles(res["buy"] + res["coming"] + res["handled"])
        shop_mod.sort_buy(res["buy"])
        res["due_soon"] = shop_mod.due_soon(res["buy"])
        return res

    def _card_titles(self, rows: list[dict]) -> None:
        """Attach the linked card (title, branch, status, due) and from it the
        effective needed-by date (shopping.apply_needed_by)."""
        cache: dict[str, dict | None] = {}
        for d in rows:
            u = d.get("card_uid")
            if not u:
                continue
            if u not in cache:
                r = self.conn.execute("SELECT uid, title, branch, status, due FROM items WHERE uid = ?",
                                      (u,)).fetchone()
                cache[u] = dict(r) if r else None
            d["card"] = cache[u]
        shop_mod.apply_needed_by(rows, self._today())

    def shop_overview(self) -> dict:
        res = shop_mod.overview(self.conn, self._now_iso(), self._today())
        self._card_titles(res["items"])
        shop_mod.sort_buy(res["items"])
        order = {d["id"]: n for n, d in enumerate(res["items"])}
        for k in res["groups"]:
            res["groups"][k].sort(key=order.get)
        res["anywhere"].sort(key=order.get)
        res["due_soon"] = shop_mod.due_soon(res["items"])
        res["suggested_run"] = shop_mod.suggested_run(res)
        # "today" is the house's day, and purchase timestamps are UTC
        day0 = dt.datetime.combine(dt.date.fromisoformat(self._today()), dt.time.min, tzinfo=self.tz)
        bounds = (day0.astimezone(dt.UTC).isoformat(timespec="seconds"),
                  (day0 + dt.timedelta(days=1)).astimezone(dt.UTC).isoformat(timespec="seconds"))
        res["bought_today"] = [dict(r) for r in self.conn.execute(
            "SELECT name, store_key, source FROM shop_purchases WHERE ts >= ? AND ts < ? "
            "AND source != 'not_low' ORDER BY ts", bounds)]
        res["errands"] = {s["key"]: len(self._errand_cards(s)) for s in res["stores"]}
        return res

    def shop_stores(self) -> list[dict]:
        return shop_mod.stores(self.conn)

    def shop_store_upsert(self, data: dict) -> dict:
        return self._tx(shop_mod.upsert_store, self.conn, self._now_iso(), data)

    def shop_store_delete(self, ref: str) -> bool:
        return self._tx(shop_mod.delete_store, self.conn, ref)

    def shop_profiles(self, q: str | None = None) -> list[dict]:
        res = shop_mod.profiles(self.conn, q)
        for p in res:
            p["cadence"] = shop_mod.cadence(self.conn, p["id"])
        return res

    def shop_profile_get(self, ref: str) -> dict | None:
        p = shop_mod.get_profile(self.conn, ref)
        if p:
            p["cadence"] = shop_mod.cadence(self.conn, p["id"])
            p["purchases"] = [dict(r) for r in self.conn.execute(
                "SELECT store_key, ts, qty, price, source, order_key FROM shop_purchases "
                "WHERE profile_id = ? AND source != 'not_low' ORDER BY ts DESC LIMIT 20", (p["id"],))]
        return p

    def shop_profile_upsert(self, data: dict) -> dict:
        return self._tx(shop_mod.upsert_profile, self.conn, self._now_iso(), data)

    def shop_profile_delete(self, ref: str) -> bool:
        return self._tx(shop_mod.delete_profile, self.conn, ref)

    def shop_purchase_add(self, data: dict) -> None:
        """History import (old orders, Groceries.md): a purchase with no list item."""
        def _do():
            p = shop_mod.get_profile(self.conn, data.get("profile") or data.get("name"))
            s = shop_mod.resolve_store(self.conn, data.get("store")) if data.get("store") else None
            if data.get("store") and s is None:
                raise StoreError(f"unknown store {data['store']!r}")
            item = {"id": None, "profile_id": p["id"] if p else None,
                    "name": data.get("name") or (p or {}).get("name") or "?", "qty": data.get("qty")}
            shop_mod.record_purchase(self.conn, self._now_iso(), item, s["key"] if s else None,
                                     data.get("price"), data.get("order_key"),
                                     data.get("source") or "import", data.get("ts"))
        self._tx(_do)

    def shop_not_low(self, profile_ref: str) -> None:
        self._tx(shop_mod.not_low, self.conn, self._now_iso(), profile_ref)

    def shop_questions(self) -> list[dict]:
        return shop_mod.open_questions(self.conn)

    def shop_answer(self, qid: str, item_ref: str | None) -> dict:
        def _do():
            q = self.conn.execute("SELECT order_key FROM shop_questions WHERE id = ?", (qid,)).fetchone()
            order = orders_mod.find(self.conn, q["order_key"]) if q else None
            res = shop_mod.answer_question(self.conn, self._now_iso(), qid, item_ref, order)
            if res.get("linked") and order and not order.get("card_uid"):
                it = shop_mod.get_item(self.conn, res["linked"])
                if it and it.get("card_uid"):
                    orders_mod.update_fields(self.conn, self._now_iso(), order["key"],
                                             {"card_uid": it["card_uid"]})
            return res
        return self._tx(_do)

    def shop_link_order(self, item_ref: str, order_ref: str) -> dict:
        def _do():
            order = orders_mod.find(self.conn, order_ref)
            if order is None:
                raise StoreError(f"no order matches {order_ref!r}")
            it = shop_mod.link_item_order(self.conn, self._now_iso(), item_ref, order)
            if it.get("card_uid") and not order.get("card_uid"):
                orders_mod.update_fields(self.conn, self._now_iso(), order["key"],
                                         {"card_uid": it["card_uid"]})
            return it
        return self._tx(_do)

    def _link_with_card(self, conn, now, order):
        sh = shop_mod.link_order(conn, now, order)
        if sh.get("card_uid"):
            orders_mod.update_fields(conn, now, order["key"], {"card_uid": sh["card_uid"]})
        return sh

    def shop_sync_orders(self) -> dict:
        return self._tx(shop_mod.sync_orders, self.conn, self._now_iso(), self._link_with_card)

    def shop_restock(self) -> list[str]:
        """REPEAT cards: bought items come back as the next cycle nears."""
        def _do():
            uids = {r["card_uid"] for r in self.conn.execute(
                "SELECT DISTINCT card_uid FROM shop_items WHERE status = 'done' AND card_uid IS NOT NULL")}
            cards = {}
            for r in self.items(uids=uids):
                cyc = [ts for ts, tx in r["notes"] if tx.startswith("done; next due")]
                cards[r["uid"]] = {"status": r["status"], "due": r.get("due"),
                                   "repeat": r.get("repeat"), "last_cycle": max(cyc) if cyc else None}
            return shop_mod.restock_from_cards(self.conn, self._now_iso(), self._today(), cards)
        return self._tx(_do)

    def clear_block(self, uid: str, seq: int, by: str = "me") -> dict | None:
        """A human clears one block (the only way a text block clears)."""
        return self._tx(self._clear_block, uid, seq, by)

    def _clear_block(self, uid: str, seq: int, by: str) -> dict | None:
        cur = self.conn.execute(
            "UPDATE blocks SET cleared_at = ?, cleared_by = ? "
            "WHERE item_uid = ? AND seq = ? AND cleared_at IS NULL",
            (self._now_iso(), f"cleared by {by}", uid, seq),
        )
        if cur.rowcount == 0:
            raise StoreError("no such open block")
        self.conn.execute(
            "UPDATE items SET updated_at = ? WHERE uid = ?", (self._now_iso(), uid)
        )
        return self._free_if_clear(uid)

    def _snapshot(self, uid: str) -> tuple | None:
        r = self.get(uid)
        if r is None:
            return None
        return tuple(r.get(c) for c in ITEM_COLS) + (tuple(r["notes"]),)

    # ----------------------------------------------------------------- writes
    def create(
        self,
        branch: str,
        title: str,
        status: str = "needs_action",
        due: str | None = None,
        description: str | None = None,
        uid: str | None = None,
        completed: str | None = None,
        source: str = "todo",
    ) -> str:
        return self._tx(
            self._create, branch, title, status, due, description, uid, completed, source
        )

    def _create(self, branch, title, status, due, description, uid, completed, source="todo") -> str:
        uid = uid or str(uuid.uuid1())
        if self.conn.execute("SELECT 1 FROM items WHERE uid = ?", (uid,)).fetchone():
            raise StoreError(f"uid {uid} already exists")
        pos = self.conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM items WHERE branch = ?",
            (branch,),
        ).fetchone()[0]
        now = self._now_iso()
        row = {
            "uid": uid,
            "branch": branch,
            "position": pos,
            "title": title or "",
            "status": status or "needs_action",
            "due": due,
            "created_at": now,
            "updated_at": now,
            "completed_at": (completed or now) if status == "completed" else None,
        }
        notes = self._apply_description(row, description)
        self._write_row(row)
        self._write_notes(uid, notes, [], [], stamp_new=True)
        self._reindex_links(row)
        self._reindex_tags(row)
        self._reindex_blocks(row)
        self._history(uid, source, "created", None, row["title"])
        return uid

    def update_from_todo(
        self,
        uid: str,
        title: str | None,
        status: str | None,
        due: str | None,
        description: str | None,
        completed: str | None = None,
        source: str = "todo",
    ) -> bool:
        """Apply a to-do UI / service edit. Returns True when something changed."""
        return self._tx(
            self._update_from_todo, uid, title, status, due, description, completed, source
        )

    def _update_from_todo(self, uid, title, status, due, description, completed, source="todo") -> bool:
        old = self.get(uid)
        if old is None:
            raise StoreError(f"no item {uid}")
        before = self._snapshot(uid)
        row = {k: old[k] for k in ITEM_COLS}
        row["title"] = title if title is not None else old["title"]
        row["due"] = due
        new_status = status or old["status"]
        if new_status != old["status"]:
            row["completed_at"] = (
                (completed or self._now_iso()) if new_status == "completed" else None
            )
        row["status"] = new_status
        if description != self.describe(old):
            notes = self._apply_description(row, description)
        else:
            notes = old["notes"]
        repeated = False
        if row["status"] == "completed" and old["status"] != "completed":
            notes = list(notes)
            repeated = self._apply_repeat(row, old, notes)
        after = tuple(row.get(c) for c in ITEM_COLS) + (tuple(notes),)
        if after == before:
            return False
        row["updated_at"] = self._now_iso()
        prev_blocks = self._blocks_state(uid)
        self._write_row(row)
        self._write_notes(uid, notes, old["notes"], old["note_added"], stamp_new=True)
        self._reindex_links(row)
        self._reindex_tags(row)
        self._reindex_blocks(row, prev_blocks)
        self._history_diff(uid, old, row, source)
        if repeated:
            self._history(uid, "system:repeat", "status", "completed", "needs_action")
        for n in notes[len(old["notes"]):]:
            self._history(uid, source, "note", None, f"{n[0]} — {n[1]}")
        return True

    EDITABLE = {"title", "status", "due", "branch", "details", *COLUMNS}

    def update_fields(self, uid: str, changes: dict, source: str = "ws") -> tuple[str, str]:
        """Column-level edit (WS API). Returns (old_branch, new_branch)."""
        return self._tx(self._update_fields, uid, changes, source)

    def _update_fields(self, uid: str, changes: dict, source: str = "ws") -> tuple[str, str]:
        bad = set(changes) - self.EDITABLE
        if bad:
            raise StoreError(f"not editable: {sorted(bad)}")
        old = self.get(uid)
        if old is None:
            raise StoreError(f"no item {uid}")
        if old["format"] == "raw" and set(changes) & {"details", *COLUMNS}:
            raise StoreError(
                "item is stored raw (pre-lean text); edit its description instead"
            )
        row = {k: old[k] for k in ITEM_COLS}
        for k, v in changes.items():
            row[k] = None if (v is None or (k in COLUMNS and v == "")) else v
        if "details" in changes and row["details"] is None:
            row["details"] = ""
        if row["status"] not in ("needs_action", "completed"):
            raise StoreError("status must be needs_action or completed")
        if row["status"] != old["status"]:
            row["completed_at"] = (
                self._now_iso() if row["status"] == "completed" else None
            )
        notes = list(old["notes"])
        repeated = False
        if row["status"] == "completed" and old["status"] != "completed":
            repeated = self._apply_repeat(row, old, notes)
        if row["branch"] != old["branch"]:
            row["position"] = self.conn.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 FROM items WHERE branch = ?",
                (row["branch"],),
            ).fetchone()[0]
        row["updated_at"] = self._now_iso()
        prev_blocks = self._blocks_state(uid)
        self._write_row(row)
        self._write_notes(
            uid, notes, old["notes"], old["note_added"], stamp_new=len(notes) > len(old["notes"])
        )
        self._reindex_links(row)
        self._reindex_tags(row)
        self._reindex_blocks(row, prev_blocks)
        self._history_diff(uid, old, row, source)
        if repeated:
            self._history(uid, "system:repeat", "status", "completed", "needs_action")
        for n in notes[len(old["notes"]):]:
            self._history(uid, source, "note", None, f"{n[0]} — {n[1]}")
        return old["branch"], row["branch"]

    def add_note(self, uid: str, text: str, ts: str | None = None, source: str = "ws") -> str:
        return self._tx(self._add_note, uid, text, ts, source)

    def _add_note(self, uid: str, text: str, ts: str | None, source: str = "ws") -> str:
        old = self.get(uid)
        if old is None:
            raise StoreError(f"no item {uid}")
        if old["format"] == "raw":
            raise StoreError("item is stored raw (pre-lean text); edit its description")
        text = (text or "").strip()
        if not text:
            raise StoreError("empty note")
        ts = ts or self._today()
        dt.date.fromisoformat(ts)
        seq = len(old["notes"])
        now = self._now_iso()
        self.conn.execute(
            "INSERT INTO notes(item_uid, seq, ts, text, added_at) VALUES (?,?,?,?,?)",
            (uid, seq, ts, text, now),
        )
        self.conn.execute("UPDATE items SET updated_at = ? WHERE uid = ?", (now, uid))
        self._history(uid, source, "note", None, f"{ts} — {text}")
        return old["branch"]

    def add_link(self, from_uid: str, to_uid: str, kind: str) -> str:
        return self._tx(self._add_link, from_uid, to_uid, kind)

    def _add_link(self, from_uid: str, to_uid: str, kind: str) -> str:
        src, dst = self.resolve(from_uid), self.resolve(to_uid)
        if not src or not dst:
            raise StoreError("both uids must resolve to existing items")
        if src == dst:
            raise StoreError("an item cannot link to itself")
        old = self.get(src)
        if kind == "parent":
            if old["format"] == "raw":
                raise StoreError("item is stored raw; edit its description")
            self._update_fields(src, {"parent_uid": dst})
        elif kind == "blocked_by":
            if old["format"] == "raw":
                raise StoreError("item is stored raw; edit its description")
            cur = old.get("blocked_by") or ""
            if dst not in cur and dst[:8] not in cur:
                self._update_fields(
                    src, {"blocked_by": f"{cur}, {dst[:8]}" if cur else dst[:8]}
                )
        elif kind == "duplicate":
            self.conn.execute(
                "INSERT OR IGNORE INTO links(from_uid, to_uid, kind, origin, created_at) "
                "VALUES (?,?,?,'api',?)",
                (src, dst, kind, self._now_iso()),
            )
        else:
            raise StoreError("kind must be parent, blocked_by or duplicate")
        return old["branch"]

    def delete(self, uids: list[str]) -> set[str]:
        return self._tx(self._delete, uids)

    def _delete(self, uids: list[str]) -> set[str]:
        touched: set[str] = set()
        now = self._now_iso()
        for row in self.items(uids=uids):
            payload = {k: row[k] for k in ITEM_COLS}
            payload["notes"] = row["notes"]
            payload["description"] = self.describe(row)
            self.conn.execute(
                "INSERT INTO deleted_items(uid, branch, payload, deleted_at) VALUES (?,?,?,?)",
                (row["uid"], row["branch"], json.dumps(payload, ensure_ascii=False), now),
            )
            self.conn.execute("DELETE FROM items WHERE uid = ?", (row["uid"],))
            self._history(row["uid"], "todo", "deleted", row["title"], None)
            touched.add(row["branch"])
        return touched

    def move(self, branch: str, uid: str, previous_uid: str | None) -> None:
        self._tx(self._move, branch, uid, previous_uid)

    def _move(self, branch: str, uid: str, previous_uid: str | None) -> None:
        order = [
            r["uid"]
            for r in self.conn.execute(
                "SELECT uid FROM items WHERE branch = ? ORDER BY position", (branch,)
            )
        ]
        if uid not in order:
            raise StoreError(f"item {uid} not in {branch}")
        if previous_uid and previous_uid not in order:
            raise StoreError(f"item {previous_uid} not in {branch}")
        order.remove(uid)
        order.insert(order.index(previous_uid) + 1 if previous_uid else 0, uid)
        for pos, u in enumerate(order):
            self.conn.execute("UPDATE items SET position = ? WHERE uid = ?", (pos, u))

    # ---------------------------------------------------------------- import
    def _noon(self, day: str) -> str:
        d = dt.date.fromisoformat(day)
        return (
            dt.datetime.combine(d, dt.time(12), self.tz)
            .astimezone(dt.UTC)
            .isoformat(timespec="seconds")
        )

    def plan_import(
        self, source: dict[str, list[dict]], force_raw: Iterable[str] = ()
    ) -> tuple[list[tuple[dict, list]], dict]:
        """Build rows from to-do items without writing. Returns (rows, report)."""
        force = {f.strip().lower() for f in force_raw if f and f.strip()}
        now = self._now_iso()
        rows: list[tuple[dict, list]] = []
        report: dict = {"branches": {}, "normalized": [], "raw": [], "errors": []}
        seen: set[str] = set()
        for branch, items in source.items():
            stats = Counter(items=0, open=0, exact=0, normalized=0, raw=0)
            for pos, it in enumerate(items):
                uid = it.get("uid")
                if not uid:
                    report["errors"].append({"branch": branch, "error": "item without uid", "title": it.get("summary")})
                    continue
                if uid in seen:
                    report["errors"].append({"branch": branch, "uid": uid, "error": "duplicate uid"})
                    continue
                seen.add(uid)
                desc = it.get("description")
                kind, card = classify(desc)
                if kind == "normalized" and it.get("status") == "completed":
                    kind = "raw"  # archive: keep closed cards' text as written
                if any(uid.lower().startswith(f) for f in force):
                    kind = "raw"
                row = {
                    "uid": uid,
                    "branch": branch,
                    "position": pos,
                    "title": it.get("summary") or "",
                    "status": it.get("status") or "needs_action",
                    "due": it.get("due"),
                    "completed_at": it.get("completed"),
                }
                if kind == "raw":
                    row.update({c: None for c in COLUMNS})
                    row["format"], row["details"], notes = "raw", desc or "", []
                else:
                    row.update({c: None for c in COLUMNS})
                    row.update(card.headers)
                    row["format"], row["details"], notes = "lean", card.details, card.notes
                created = uuid1_created(uid)
                dates = sorted(n[0] for n in notes)
                if created is not None:
                    row["created_at"] = created.isoformat(timespec="seconds")
                elif dates:
                    row["created_at"] = self._noon(dates[0])
                else:
                    row["created_at"] = now
                row["updated_at"] = self._noon(dates[-1]) if dates else row["created_at"]
                rows.append((row, notes))
                stats["items"] += 1
                stats["open"] += row["status"] == "needs_action"
                stats[kind] += 1
                if kind != "exact":
                    projected = self.describe({**row, "notes": notes})
                    entry = {
                        "branch": branch,
                        "uid": uid,
                        "status": row["status"],
                        "title": row["title"],
                        "diff": "\n".join(
                            difflib.unified_diff(
                                (desc or "").split("\n"),
                                (projected or "").split("\n"),
                                "current",
                                "projected",
                                lineterm="",
                                n=1,
                            )
                        )
                        if kind == "normalized"
                        else "",
                    }
                    report[kind].append(entry)
            report["branches"][branch] = dict(stats)
        return rows, report

    def import_rows(
        self, rows: list[tuple[dict, list]], branches: Iterable[str], replace: bool
    ) -> None:
        self._tx(self._import_rows, rows, list(branches), replace)

    def _import_rows(self, rows, branches, replace) -> None:
        for b in branches:
            n = self.conn.execute(
                "SELECT COUNT(*) FROM items WHERE branch = ?", (b,)
            ).fetchone()[0]
            if n and not replace:
                raise StoreError(
                    f"branch {b} already holds {n} items; pass replace to overwrite"
                )
            self.conn.execute("DELETE FROM items WHERE branch = ?", (b,))
        for row, notes in rows:
            self._write_row(row)
            self._write_notes(row["uid"], notes, [], [], stamp_new=False)
        for row, _ in rows:
            self._reindex_links(row)
            self._reindex_tags(row)
        self._reindex_blocks(row)
        self.conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('imported_at', ?)",
            (self._now_iso(),),
        )

    # ------------------------------------------------------- calendar sync
    def due_items(self) -> list[dict]:
        """Open cards with a due date — what the calendar should show."""
        rows = self.conn.execute(
            "SELECT uid, branch, title, due, next_step FROM items "
            "WHERE status = 'needs_action' AND due IS NOT NULL AND due != '' "
            "ORDER BY due, branch, position"
        )
        return [dict(r) for r in rows]

    def calendar_rows(self) -> dict[str, dict]:
        return {
            r["item_uid"]: dict(r)
            for r in self.conn.execute("SELECT * FROM calendar_events")
        }

    def calendar_set(self, item_uid: str, event_uid: str | None, due: str, title: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO calendar_events(item_uid, event_uid, due, title) "
            "VALUES (?,?,?,?)",
            (item_uid, event_uid, due, title),
        )

    def calendar_del(self, item_uid: str) -> None:
        self.conn.execute("DELETE FROM calendar_events WHERE item_uid = ?", (item_uid,))

    # ---------------------------------------------------------------- export
    def export(self) -> dict:
        rows = self.items()
        out_items = []
        lists: dict[str, list] = {}
        for r in rows:
            item = {k: r[k] for k in ITEM_COLS}
            item["notes"] = [
                {"ts": ts, "text": tx, "added_at": a}
                for (ts, tx), a in zip(r["notes"], r["note_added"], strict=False)
            ]
            item["links"] = [
                link for link in self.links(r["uid"]) if link["from_uid"] == r["uid"]
            ]
            item["tags"] = self.branches_of(r["uid"])
            out_items.append(item)
            lists.setdefault(r["branch"], []).append(
                {
                    "uid": r["uid"],
                    "summary": r["title"],
                    "status": r["status"],
                    "due": r["due"],
                    "description": self.describe(r),
                    "completed": r["completed_at"],
                }
            )
        since = (self._now() - dt.timedelta(days=90)).isoformat(timespec="seconds")
        history = [
            dict(r)
            for r in self.conn.execute(
                "SELECT id, item_uid, ts, source, field, old, new FROM item_history "
                "WHERE ts >= ? ORDER BY id",
                (since,),
            )
        ]
        return {
            "exported_at": self._now_iso(),
            "schema_version": SCHEMA_VERSION,
            "items": out_items,
            "todo_lists": lists,
            "history": history,
            "shopping": shop_mod.export(self.conn),
        }
