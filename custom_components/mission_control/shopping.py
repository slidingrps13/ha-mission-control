"""House shopping list (card: shopping list, 2026-10-29).

One shared list for the house, kept in the Mission Control database next to the
package-tracker orders.  Synchronous, no Home Assistant imports; every write runs
inside MissionStore._tx from the executor, like the rest of the store.

Tables
  shop_stores     where things can be bought: key, name, kind (in_store|online),
                  the categories it carries, ``general`` (also takes uncategorised
                  items), the HA zone that marks being there, aliases.
  shop_profiles   what an item IS: canonical name, category, aliases (the house's
                  words for it), bulk/top-up store, star (deal watch).
  shop_prefs      per profile *per store*: brand, size, last price + when it was
                  read, product url.  Never merged across stores.
  shop_purchases  every buy (check-off, delivered order, import) — the restock
                  cadence and price history come from here.
  shop_items      the list itself.  status: needed -> ordered -> arriving -> done,
                  or dismissed.  ``handled`` (e.g. "Subscribe & Save") keeps an
                  item visible but out of the to-buy pile.
  shop_questions  "was this order the dish soap?" — asked when an order email is
                  too vague to match (Amazon personal: "1 Kitchen item").

Matching an item to a store (``item_at_store``), first rule that applies:
  1. the item names stores explicitly -> only those;
  2. its profile has a preference at that store -> yes;
  3. its category is one the store carries -> yes;
  4. no category -> only ``general`` stores.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import statistics
import uuid

SCHEMA = """
CREATE TABLE IF NOT EXISTS shop_stores (
  key TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'in_store',
  categories TEXT NOT NULL DEFAULT '[]',
  general INTEGER NOT NULL DEFAULT 0,
  zone TEXT,
  address TEXT,
  aliases TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shop_profiles (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  category TEXT,
  aliases TEXT NOT NULL DEFAULT '[]',
  bulk_store TEXT,
  topup_store TEXT,
  star INTEGER NOT NULL DEFAULT 0,
  handled TEXT,
  notes TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shop_prefs (
  profile_id TEXT NOT NULL REFERENCES shop_profiles(id) ON DELETE CASCADE,
  store_key TEXT NOT NULL,
  brand TEXT,
  size TEXT,
  price REAL,
  price_at TEXT,
  url TEXT,
  note TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (profile_id, store_key)
);
CREATE TABLE IF NOT EXISTS shop_purchases (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  profile_id TEXT,
  item_id TEXT,
  name TEXT NOT NULL,
  store_key TEXT,
  ts TEXT NOT NULL,
  qty TEXT,
  price REAL,
  order_key TEXT,
  source TEXT
);
CREATE INDEX IF NOT EXISTS shop_purchases_profile ON shop_purchases(profile_id, ts);
CREATE TABLE IF NOT EXISTS shop_items (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  qty TEXT,
  note TEXT,
  category TEXT,
  stores TEXT NOT NULL DEFAULT '[]',
  profile_id TEXT,
  card_uid TEXT,
  status TEXT NOT NULL DEFAULT 'needed',
  order_key TEXT,
  handled TEXT,
  added_by TEXT,
  source TEXT,
  position INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  done_at TEXT,
  needed_by TEXT
);
CREATE INDEX IF NOT EXISTS shop_items_status ON shop_items(status);
CREATE INDEX IF NOT EXISTS shop_items_order ON shop_items(order_key);
CREATE TABLE IF NOT EXISTS shop_questions (
  id TEXT PRIMARY KEY,
  order_key TEXT NOT NULL,
  candidates TEXT NOT NULL,
  created_at TEXT NOT NULL,
  answered_at TEXT,
  answer TEXT
);
"""

STATUSES = ("needed", "ordered", "arriving", "done", "dismissed")
OPEN = ("needed", "ordered", "arriving")
CATEGORIES = (
    "produce", "meat", "dairy", "bakery", "frozen", "pantry", "beverages", "snacks",
    "household", "cleaning", "paper", "personal_care", "pharmacy", "baby", "pet",
    "kitchen", "office", "electronics", "smart_home", "hardware", "tools", "electrical",
    "plumbing", "paint", "garden", "outdoor", "hvac", "automotive", "batteries", "lighting",
    "clothing", "home_decor",
)
# order stage -> item status
ORDER_STAGE_STATUS = {
    "ordered": "ordered",
    "shipped": "arriving",
    "out_for_delivery": "arriving",
    "delivered": "done",
    "cancelled": "needed",
}
RESTOCK_LEAD_DAYS = 14
LOW_FRACTION = 0.85  # suggest once this share of the usual gap has passed

# A cheap first guess at a category; the caller (Claude, the page) can override.
_CATEGORY_WORDS: dict[str, tuple[str, ...]] = {
    "produce": ("apple", "banana", "lettuce", "tomato", "onion", "potato", "avocado", "lemon",
                "lime", "berry", "berries", "spinach", "carrot", "pepper", "cucumber", "garlic",
                "cilantro", "herb", "fruit", "vegetable", "grape", "orange"),
    "meat": ("chicken", "beef", "pork", "steak", "turkey", "sausage", "bacon", "fish", "salmon",
             "shrimp", "ground", "lamb", "ham"),
    "dairy": ("milk", "cheese", "yogurt", "butter", "cream", "egg", "eggs", "half and half"),
    "bakery": ("bread", "bagel", "tortilla", "bun", "roll", "croissant", "muffin"),
    "frozen": ("frozen", "ice cream", "pizza"),
    "pantry": ("rice", "pasta", "flour", "sugar", "oil", "olive oil", "vinegar", "spice", "salt",
               "sauce", "cereal", "oat", "beans", "can", "canned", "soup", "honey", "coffee",
               "tea", "peanut butter", "jam", "ketchup", "mustard", "mayo", "granola",
               "hot sauce", "cholula", "salsa", "spices"),
    "beverages": ("water", "soda", "juice", "sparkling", "la croix", "gatorade", "wine", "beer",
                  "kombucha"),
    "snacks": ("chips", "crackers", "cookies", "snack", "nuts", "popcorn", "pretzel", "bar"),
    "cleaning": ("soap", "detergent", "bleach", "cleaner", "wipes", "sponge", "dish", "dishwasher",
                 "laundry", "lysol", "clorox", "windex", "trash bag", "garbage bag", "scrub",
                 "sponge", "hand cleaner"),
    "paper": ("toilet paper", "paper towel", "tissue", "napkin", "kleenex", "plates", "cups"),
    "personal_care": ("shampoo", "conditioner", "toothpaste", "toothbrush", "deodorant", "razor",
                      "lotion", "sunscreen", "floss", "body wash", "mouthwash"),
    "pharmacy": ("tylenol", "advil", "ibuprofen", "vitamin", "bandage", "band-aid", "allergy",
                 "medicine"),
    "pet": ("cat", "dog", "litter", "kibble", "pet", "treats", "flea"),
    "kitchen": ("foil", "plastic wrap", "ziploc", "zip bag", "parchment", "filter coffee",
                "pan", "knife", "utensil", "sodastream"),
    "office": ("printer", "ink", "toner", "paper ream", "label", "pen", "envelope", "stapler"),
    "electronics": ("cable", "usb", "hdmi", "charger", "adapter", "ssd", "hard drive", "mouse",
                    "keyboard", "monitor", "router", "ethernet", "ipad", "tablet", "laptop", "iphone"),
    "smart_home": ("zigbee", "z-wave", "zwave", "smart plug", "sensor", "hue", "esp32", "thermostat"),
    "hardware": ("screw", "nail", "anchor", "bolt", "hinge", "bracket", "glue", "tape", "caulk",
                 "wd-40", "zip tie", "hook"),
    "tools": ("drill", "bit", "saw", "hammer", "wrench", "screwdriver", "level", "tool"),
    "electrical": ("outlet", "switch", "wire", "breaker", "wire nut", "receptacle", "gfci",
                   "extension cord", "dimmer"),
    "plumbing": ("pvc", "pipe", "faucet", "valve", "flapper", "fill valve", "drain", "plumber",
                 "supply line", "teflon"),
    "paint": ("paint", "primer", "roller", "brush", "drop cloth", "spackle"),
    "garden": ("mulch", "soil", "fertilizer", "seed", "seeds", "play sand", "sand", "plant", "hose", "sprinkler", "weed",
               "potting"),
    "outdoor": ("propane", "charcoal", "grill"),
    "hvac": ("air filter", "hvac filter", "furnace filter", "filtrete", "20x25", "16x25",
             "purifier filter", "hepa"),
    "automotive": ("motor oil", "oil filter", "wiper", "coolant", "windshield", "tire",
                   "car wash", "brake"),
    "batteries": ("battery", "batteries", "aa", "aaa", "cr2032", "9v"),
    "lighting": ("bulb", "led bulb", "light bulb", "lamp"),
    "household": ("storage bin", "storage box", "tote", "sterilite", "hanger", "vacuum", "gasket box"),
}
_STOP = {"a", "an", "the", "of", "for", "and", "or", "with", "pack", "ct", "count", "oz", "fl",
         "lb", "lbs", "pk", "x", "set", "item", "items", "new", "size", "large", "small", "medium",
         "1", "2", "3", "4", "5", "6", "8", "10", "12", "24"}
_GENERIC_ORDER = re.compile(r"^\s*\d*\s*[\w&' -]{0,30}\bitems?\b\s*$", re.I)


# ------------------------------------------------------------------ helpers
def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(shop_items)")}
    if "needed_by" not in cols:
        # 2026-11-01: a date the item is needed by (set by hand, or inherited from the card's due)
        conn.execute("ALTER TABLE shop_items ADD COLUMN needed_by TEXT")
        conn.commit()


_DATE_RX = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _date_or_none(v) -> str | None:
    v = (v or "").strip()[:10] if isinstance(v, str) else (v.isoformat() if v else "")
    if not v:
        return None
    if not _DATE_RX.match(v):
        raise ValueError("needed_by must be YYYY-MM-DD")
    return v


def _j(v) -> list:
    if not v:
        return []
    if isinstance(v, list):
        return v
    try:
        out = json.loads(v)
    except (TypeError, ValueError):
        return []
    return out if isinstance(out, list) else []


def norm(s: str | None) -> str:
    s = (s or "").lower().replace("’", "'")
    s = re.sub(r"[^a-z0-9&' ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", norm(s)).strip("_")


def _singular(w: str) -> str:
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def tokens(s: str | None) -> set[str]:
    return {_singular(w) for w in norm(s).split() if w not in _STOP and len(w) > 1}


_FOOD = {"produce", "meat", "dairy", "bakery", "frozen", "pantry", "beverages", "snacks"}


def guess_category(name: str) -> str | None:
    """Longest matching phrase wins; a non-food match beats a food word
    ("Apple iPad" is electronics, "tomato seeds" is garden)."""
    n = f" {norm(name)} "
    hits: dict[str, int] = {}
    for cat, words in _CATEGORY_WORDS.items():
        for w in words:
            if f" {w} " in n or (len(w) > 4 and w in n):
                hits[cat] = max(hits.get(cat, 0), len(w))
    if not hits:
        return None
    nonfood = {c: v for c, v in hits.items() if c not in _FOOD}
    pool = nonfood or hits
    return max(pool, key=lambda c: pool[c])


def _price(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return round(float(str(v).replace("$", "").replace(",", "").strip()), 2)
    except ValueError:
        return None


# ------------------------------------------------------------------ stores
STORE_FIELDS = ("key", "name", "kind", "categories", "general", "zone", "address", "aliases")


def _store(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    d = {k: r[k] for k in STORE_FIELDS}
    d["categories"] = _j(d["categories"])
    d["aliases"] = _j(d["aliases"])
    d["general"] = bool(d["general"])
    return d


def stores(conn: sqlite3.Connection) -> list[dict]:
    return [_store(r) for r in conn.execute("SELECT * FROM shop_stores ORDER BY name")]


def resolve_store(conn: sqlite3.Connection, ref: str | None) -> dict | None:
    """By key, name or alias, case/punctuation-insensitive; zone entity id too."""
    if not ref:
        return None
    n = norm(ref)
    k = slug(ref)
    for s in stores(conn):
        names = {s["key"], norm(s["name"]), *(norm(a) for a in s["aliases"])}
        if k in names or n in names or (s["zone"] and ref.strip() == s["zone"]):
            return s
    # "home depot run", "lowes on broadway": a store name inside the phrase
    for s in sorted(stores(conn), key=lambda s: -len(s["name"])):
        for nm in (norm(s["name"]), *(norm(a) for a in s["aliases"])):
            if nm and re.search(rf"\b{re.escape(nm)}\b", n):
                return s
    return None


def upsert_store(conn: sqlite3.Connection, now: str, data: dict) -> dict:
    name = (data.get("name") or "").strip()
    key = slug(data.get("key") or name)
    if not key:
        raise ValueError("store needs a name")
    cur = _store(conn.execute("SELECT * FROM shop_stores WHERE key = ?", (key,)).fetchone())
    new = cur or {"key": key, "name": name or key, "kind": "in_store", "categories": [],
                  "general": False, "zone": None, "address": None, "aliases": []}
    for f in ("name", "kind", "zone", "address"):
        if data.get(f) is not None:
            new[f] = data[f] or None if f != "name" else data[f]
    if new["kind"] not in ("in_store", "online"):
        raise ValueError("kind is in_store or online")
    if data.get("categories") is not None:
        cats = [c for c in data["categories"] if c]
        bad = [c for c in cats if c not in CATEGORIES]
        if bad:
            raise ValueError(f"unknown categories {bad}; known: {', '.join(CATEGORIES)}")
        new["categories"] = cats
    for c in data.get("add_categories") or []:
        if c not in CATEGORIES:
            raise ValueError(f"unknown category {c}")
        if c not in new["categories"]:
            new["categories"].append(c)
    if data.get("aliases") is not None:
        new["aliases"] = [a for a in data["aliases"] if a]
    if data.get("general") is not None:
        new["general"] = bool(data["general"])
    conn.execute(
        "INSERT OR REPLACE INTO shop_stores(key,name,kind,categories,general,zone,address,aliases,"
        "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,COALESCE((SELECT created_at FROM "
        "shop_stores WHERE key = ?), ?),?)",
        (key, new["name"], new["kind"], json.dumps(new["categories"]), 1 if new["general"] else 0,
         new["zone"], new["address"], json.dumps(new["aliases"]), key, now, now),
    )
    return _store(conn.execute("SELECT * FROM shop_stores WHERE key = ?", (key,)).fetchone())


def delete_store(conn: sqlite3.Connection, ref: str) -> bool:
    s = resolve_store(conn, ref)
    if s is None:
        return False
    conn.execute("DELETE FROM shop_stores WHERE key = ?", (s["key"],))
    return True


# ---------------------------------------------------------------- profiles
PROFILE_FIELDS = ("id", "name", "category", "aliases", "bulk_store", "topup_store", "star",
                  "handled", "notes")


def _profile(conn: sqlite3.Connection, r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    d = {k: r[k] for k in PROFILE_FIELDS}
    d["aliases"] = _j(d["aliases"])
    d["star"] = bool(d["star"])
    d["prefs"] = {
        p["store_key"]: {k: p[k] for k in ("brand", "size", "price", "price_at", "url", "note")}
        for p in conn.execute("SELECT * FROM shop_prefs WHERE profile_id = ?", (d["id"],))
    }
    return d


def get_profile(conn: sqlite3.Connection, ref: str | None) -> dict | None:
    """By id, exact name or alias (normalized)."""
    if not ref:
        return None
    r = conn.execute("SELECT * FROM shop_profiles WHERE id = ?", (ref,)).fetchone()
    if r is not None:
        return _profile(conn, r)
    n = norm(ref)
    ns = " ".join(sorted(tokens(ref)))
    for r in conn.execute("SELECT * FROM shop_profiles"):
        names = [r["name"], *_j(r["aliases"])]
        if any(norm(x) == n for x in names) or (
            ns and any(" ".join(sorted(tokens(x))) == ns for x in names)
        ):
            return _profile(conn, r)
    return None


def profiles(conn: sqlite3.Connection, q: str | None = None) -> list[dict]:
    rows = [_profile(conn, r) for r in conn.execute("SELECT * FROM shop_profiles ORDER BY name")]
    if q:
        qt = tokens(q)
        rows = [p for p in rows if qt & tokens(" ".join([p["name"], *p["aliases"]]))]
    return rows


def upsert_profile(conn: sqlite3.Connection, now: str, data: dict) -> dict:
    """Create or update a profile.  ``prefs``: {store: {brand,size,price,url,note}} merges per store.

    A price given here is stamped ``price_at`` = now unless ``price_at`` is passed.
    """
    cur = get_profile(conn, data.get("id") or data.get("name"))
    if cur is None:
        name = (data.get("name") or "").strip()
        if not name:
            raise ValueError("profile needs a name")
        cur = {"id": uuid.uuid4().hex[:12], "name": name, "category": None, "aliases": [],
               "bulk_store": None, "topup_store": None, "star": False, "handled": None,
               "notes": None, "prefs": {}}
        created = now
    else:
        created = None
    new = dict(cur)
    if data.get("name") and data.get("id"):
        new["name"] = data["name"].strip()
    if "category" in data:
        c = data["category"] or None
        if c and c not in CATEGORIES:
            raise ValueError(f"unknown category {c}")
        new["category"] = c
    elif new["category"] is None:
        new["category"] = guess_category(new["name"])
    if data.get("aliases") is not None:
        new["aliases"] = [a for a in data["aliases"] if a]
    for a in data.get("add_aliases") or []:
        if a and norm(a) not in {norm(x) for x in new["aliases"]} and norm(a) != norm(new["name"]):
            new["aliases"].append(a)
    for f in ("bulk_store", "topup_store"):
        if f in data:
            s = resolve_store(conn, data[f]) if data[f] else None
            if data[f] and s is None:
                raise ValueError(f"unknown store {data[f]!r}")
            new[f] = s["key"] if s else None
    for f in ("handled", "notes"):
        if f in data:
            new[f] = data[f] or None
    if "star" in data:
        new["star"] = bool(data["star"])
    conn.execute(
        "INSERT OR REPLACE INTO shop_profiles(id,name,category,aliases,bulk_store,topup_store,star,"
        "handled,notes,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,"
        "COALESCE(?, (SELECT created_at FROM shop_profiles WHERE id = ?), ?),?)",
        (new["id"], new["name"], new["category"], json.dumps(new["aliases"]), new["bulk_store"],
         new["topup_store"], 1 if new["star"] else 0, new["handled"], new["notes"], created,
         new["id"], now, now),
    )
    for sref, pref in (data.get("prefs") or {}).items():
        s = resolve_store(conn, sref)
        if s is None:
            raise ValueError(f"unknown store {sref!r}")
        set_pref(conn, now, new["id"], s["key"], pref or {})
    return get_profile(conn, new["id"])


def set_pref(conn: sqlite3.Connection, now: str, profile_id: str, store_key: str, pref: dict) -> None:
    old = conn.execute(
        "SELECT * FROM shop_prefs WHERE profile_id = ? AND store_key = ?", (profile_id, store_key)
    ).fetchone()
    row = {k: (old[k] if old else None) for k in ("brand", "size", "price", "price_at", "url", "note")}
    for k in ("brand", "size", "url", "note"):
        if k in pref:
            row[k] = (pref[k] or None)
    if "price" in pref:
        row["price"] = _price(pref["price"])
        row["price_at"] = pref.get("price_at") or (now if row["price"] is not None else None)
    conn.execute(
        "INSERT OR REPLACE INTO shop_prefs(profile_id,store_key,brand,size,price,price_at,url,note,"
        "updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (profile_id, store_key, row["brand"], row["size"], row["price"], row["price_at"],
         row["url"], row["note"], now),
    )


def delete_profile(conn: sqlite3.Connection, ref: str) -> bool:
    p = get_profile(conn, ref)
    if p is None:
        return False
    conn.execute("DELETE FROM shop_prefs WHERE profile_id = ?", (p["id"],))
    conn.execute("DELETE FROM shop_profiles WHERE id = ?", (p["id"],))
    conn.execute("UPDATE shop_items SET profile_id = NULL WHERE profile_id = ?", (p["id"],))
    return True


# ------------------------------------------------------------------- items
ITEM_FIELDS = ("id", "name", "qty", "note", "category", "stores", "profile_id", "card_uid",
               "status", "order_key", "handled", "added_by", "source", "position", "created_at",
               "updated_at", "done_at", "needed_by")


def _item(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    d = {k: r[k] for k in ITEM_FIELDS}
    d["stores"] = _j(d["stores"])
    return d


def get_item(conn: sqlite3.Connection, ref: str) -> dict | None:
    """By id (or a unique id prefix of 6+), else by name among open items."""
    ref = (ref or "").strip()
    if not ref:
        return None
    r = conn.execute("SELECT * FROM shop_items WHERE id = ?", (ref,)).fetchone()
    if r is None and len(ref) >= 6 and re.fullmatch(r"[0-9a-f]+", ref):
        rows = conn.execute("SELECT * FROM shop_items WHERE id LIKE ?", (ref + "%",)).fetchall()
        r = rows[0] if len(rows) == 1 else None
    if r is None:
        n = " ".join(sorted(tokens(ref)))
        for row in conn.execute(
            "SELECT * FROM shop_items WHERE status IN ('needed','ordered','arriving') "
            "ORDER BY created_at DESC"
        ):
            if norm(row["name"]) == norm(ref) or (n and " ".join(sorted(tokens(row["name"]))) == n):
                r = row
                break
    return _item(r)


def _resolve_store_list(conn: sqlite3.Connection, refs) -> list[str]:
    if refs is None:
        return []
    if isinstance(refs, str):
        refs = [x for x in re.split(r"[,;/]", refs) if x.strip()]
    out = []
    for ref in refs:
        s = resolve_store(conn, ref)
        if s is None:
            raise ValueError(f"unknown store {ref!r} — add it first (shopping/stores/upsert)")
        if s["key"] not in out:
            out.append(s["key"])
    return out


def add_item(conn: sqlite3.Connection, now: str, data: dict) -> dict:
    """Add to the list.  An open item with the same name is merged, not duplicated.

    Returns {item, merged}.  ``card_uid`` must already be resolved by the caller.
    """
    name = (data.get("name") or "").strip()
    if not name:
        raise ValueError("item needs a name")
    profile = get_profile(conn, data.get("profile_id") or name)
    existing = get_item(conn, name)
    if existing and existing["status"] == "needed":
        sets = {}
        if data.get("qty"):
            sets["qty"] = data["qty"]
        if data.get("note"):
            sets["note"] = "; ".join(x for x in (existing["note"], data["note"]) if x)
        if data.get("card_uid") and not existing["card_uid"]:
            sets["card_uid"] = data["card_uid"]
        if data.get("stores"):
            sets["stores"] = json.dumps(
                list(dict.fromkeys(existing["stores"] + _resolve_store_list(conn, data["stores"]))))
        nb = _date_or_none(data.get("needed_by"))
        if nb and (not existing["needed_by"] or nb < existing["needed_by"]):
            sets["needed_by"] = nb
        if sets:
            sets["updated_at"] = now
            conn.execute(f"UPDATE shop_items SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?",
                         [*sets.values(), existing["id"]])
        return {"item": get_item(conn, existing["id"]), "merged": True}
    cat = data.get("category") or (profile or {}).get("category") or guess_category(name)
    if cat and cat not in CATEGORIES:
        raise ValueError(f"unknown category {cat}")
    pos = conn.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM shop_items").fetchone()[0]
    iid = uuid.uuid4().hex[:12]
    conn.execute(
        "INSERT INTO shop_items(id,name,qty,note,category,stores,profile_id,card_uid,status,"
        "order_key,handled,added_by,source,position,created_at,updated_at,done_at,needed_by) "
        "VALUES (?,?,?,?,?,?,?,?,'needed',NULL,?,?,?,?,?,?,NULL,?)",
        (iid, (profile or {}).get("name") or name if data.get("use_profile_name") else name,
         data.get("qty") or None, data.get("note") or None, cat,
         json.dumps(_resolve_store_list(conn, data.get("stores"))),
         (profile or {}).get("id"), data.get("card_uid") or None,
         data.get("handled") or (profile or {}).get("handled"),
         data.get("added_by") or None, data.get("source") or None, pos, now, now,
         _date_or_none(data.get("needed_by"))),
    )
    return {"item": get_item(conn, iid), "merged": False}


def update_item(conn: sqlite3.Connection, now: str, ref: str, fields: dict) -> dict:
    it = get_item(conn, ref)
    if it is None:
        raise ValueError(f"no shopping item matches {ref!r}")
    sets: dict = {}
    for f in ("name", "qty", "note", "handled", "added_by"):
        if f in fields:
            sets[f] = (fields[f] or None) if f != "name" else (fields[f] or it["name"])
    if "category" in fields:
        c = fields["category"] or None
        if c and c not in CATEGORIES:
            raise ValueError(f"unknown category {c}")
        sets["category"] = c
    if "stores" in fields:
        sets["stores"] = json.dumps(_resolve_store_list(conn, fields["stores"]))
    if "card_uid" in fields:
        sets["card_uid"] = fields["card_uid"] or None
    if "needed_by" in fields:
        sets["needed_by"] = _date_or_none(fields["needed_by"])
    if "profile" in fields:
        p = get_profile(conn, fields["profile"]) if fields["profile"] else None
        if fields["profile"] and p is None:
            raise ValueError(f"no profile {fields['profile']!r}")
        sets["profile_id"] = p["id"] if p else None
    if "status" in fields:
        st = fields["status"]
        if st not in STATUSES:
            raise ValueError(f"status is one of {STATUSES}")
        sets["status"] = st
        sets["done_at"] = now if st in ("done", "dismissed") else None
        if st == "needed":
            sets["order_key"] = None
    if not sets:
        return it
    sets["updated_at"] = now
    conn.execute(f"UPDATE shop_items SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?",
                 [*sets.values(), it["id"]])
    return get_item(conn, it["id"])


def record_purchase(conn: sqlite3.Connection, now: str, item: dict, store_key: str | None,
                    price=None, order_key: str | None = None, source: str = "check",
                    ts: str | None = None) -> None:
    conn.execute(
        "INSERT INTO shop_purchases(profile_id,item_id,name,store_key,ts,qty,price,order_key,source)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (item.get("profile_id"), item.get("id"), item["name"], store_key, ts or now,
         item.get("qty"), _price(price), order_key, source),
    )
    if item.get("profile_id") and store_key and _price(price) is not None:
        set_pref(conn, now, item["profile_id"], store_key, {"price": price})


def check_item(conn: sqlite3.Connection, now: str, ref: str, store: str | None = None,
               price=None, source: str = "check") -> dict:
    """Bought it (in store, or by hand).  Records the purchase for cadence + price."""
    it = get_item(conn, ref)
    if it is None:
        raise ValueError(f"no shopping item matches {ref!r}")
    s = resolve_store(conn, store) if store else None
    if store and s is None:
        raise ValueError(f"unknown store {store!r}")
    if s is None and len(it["stores"]) == 1:
        s = {"key": it["stores"][0]}
    if it["status"] != "done":
        record_purchase(conn, now, it, s["key"] if s else None, price, it["order_key"], source)
    return update_item(conn, now, it["id"], {"status": "done"})


def uncheck_item(conn: sqlite3.Connection, now: str, ref: str) -> dict:
    """Undo a check-off: back to needed, the purchase row is dropped."""
    it = get_item(conn, ref)
    if it is None:
        raise ValueError(f"no shopping item matches {ref!r}")
    if it["status"] == "done":
        conn.execute(
            "DELETE FROM shop_purchases WHERE id = (SELECT id FROM shop_purchases WHERE item_id = ? "
            "ORDER BY id DESC LIMIT 1)", (it["id"],))
    return update_item(conn, now, it["id"], {"status": "needed"})


def items(conn: sqlite3.Connection, status: str = "open", done_days: int = 2,
          now: str | None = None) -> list[dict]:
    """status: open (needed/ordered/arriving + done within done_days), all, or one status."""
    if status == "all":
        rows = conn.execute("SELECT * FROM shop_items ORDER BY position").fetchall()
    elif status == "open":
        since = ""
        if now:
            since = (dt.datetime.fromisoformat(now) - dt.timedelta(days=done_days)).isoformat(
                timespec="seconds")
        rows = conn.execute(
            "SELECT * FROM shop_items WHERE status IN ('needed','ordered','arriving') "
            "OR (status = 'done' AND done_at >= ?) ORDER BY position", (since or "9999",)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM shop_items WHERE status = ? ORDER BY position",
                            (status,)).fetchall()
    return [_item(r) for r in rows]


# ---------------------------------------------------------- store matching
def item_at_store(item: dict, store: dict, profile: dict | None) -> bool:
    if item.get("stores"):
        return store["key"] in item["stores"]
    if profile and store["key"] in (profile.get("prefs") or {}):
        return True
    if profile and store["key"] in (profile.get("bulk_store"), profile.get("topup_store")):
        return True
    cat = item.get("category") or (profile or {}).get("category")
    if cat:
        return cat in store["categories"]
    return store["general"]


def _profiles_by_id(conn: sqlite3.Connection) -> dict[str, dict]:
    return {p["id"]: p for p in profiles(conn)}


def _decorate(item: dict, profile: dict | None, store: dict | None, all_stores: dict) -> dict:
    """Add what the page/voice needs: brand/size/price here, cheaper elsewhere, bulk hint."""
    out = dict(item)
    prefs = (profile or {}).get("prefs") or {}
    here = prefs.get(store["key"]) if store else None
    out["profile"] = ({"id": profile["id"], "name": profile["name"], "star": profile["star"]}
                      if profile else None)
    out["pref"] = here
    out["est_price"] = (here or {}).get("price")
    if store and here and here.get("price") is not None:
        cheaper = [
            (k, p["price"]) for k, p in prefs.items()
            if k != store["key"] and p.get("price") is not None and p["price"] < here["price"] * 0.9
        ]
        if cheaper:
            k, pr = min(cheaper, key=lambda x: x[1])
            out["cheaper_at"] = {"store": k, "name": all_stores.get(k, {}).get("name", k), "price": pr}
    if store and profile and profile.get("bulk_store") and profile["bulk_store"] != store["key"]:
        out["bulk_at"] = {"store": profile["bulk_store"],
                          "name": all_stores.get(profile["bulk_store"], {}).get("name",
                                                                                profile["bulk_store"])}
    if not store and prefs:
        priced = [p["price"] for p in prefs.values() if p.get("price") is not None]
        out["est_price"] = min(priced) if priced else None
    return out


def cadence(conn: sqlite3.Connection, profile_id: str) -> dict | None:
    """Median gap between purchases (days) and the last one; None under 2 buys."""
    ts = [r["ts"][:10] for r in conn.execute(
        "SELECT ts FROM shop_purchases WHERE profile_id = ? ORDER BY ts", (profile_id,))]
    days = sorted({dt.date.fromisoformat(t) for t in ts})
    if len(days) < 2:
        return None
    gaps = [(b - a).days for a, b in zip(days, days[1:], strict=False) if (b - a).days >= 3]
    if not gaps:
        return None
    return {"interval_days": int(statistics.median(gaps)), "last": days[-1].isoformat(),
            "buys": len(days)}


def running_low(conn: sqlite3.Connection, today: str, store: dict | None = None) -> list[dict]:
    """Profiles whose usual gap has (nearly) passed and that are not on the list."""
    t = dt.date.fromisoformat(today)
    open_profiles = {r["profile_id"] for r in conn.execute(
        "SELECT profile_id FROM shop_items WHERE status IN ('needed','ordered','arriving') "
        "AND profile_id IS NOT NULL")}
    dismissed = {r["profile_id"]: r["ts"] for r in conn.execute(
        "SELECT profile_id, MAX(ts) AS ts FROM shop_purchases WHERE source = 'not_low' "
        "GROUP BY profile_id")}
    out = []
    for p in profiles(conn):
        if p["id"] in open_profiles or p.get("handled"):
            continue
        c = cadence(conn, p["id"])
        if c is None:
            continue
        since = (t - dt.date.fromisoformat(c["last"])).days
        if since < c["interval_days"] * LOW_FRACTION:
            continue
        nl = dismissed.get(p["id"])
        if nl and (t - dt.date.fromisoformat(nl[:10])).days < c["interval_days"] * 0.3:
            continue  # "not yet" said recently
        probe = {"stores": [], "category": p["category"]}
        if store and not item_at_store(probe, store, p):
            continue
        out.append({"profile": p["id"], "name": p["name"], "category": p["category"],
                    "days_since": since, **c,
                    "pref": (p["prefs"] or {}).get(store["key"]) if store else None})
    out.sort(key=lambda x: -(x["days_since"] / max(x["interval_days"], 1)))
    return out


def not_low(conn: sqlite3.Connection, now: str, profile_ref: str) -> None:
    """'Not yet' on a running-low suggestion: hush it for ~30% of the usual gap."""
    p = get_profile(conn, profile_ref)
    if p is None:
        raise ValueError(f"no profile {profile_ref!r}")
    conn.execute("INSERT INTO shop_purchases(profile_id,name,ts,source) VALUES (?,?,?,'not_low')",
                 (p["id"], p["name"], now))


def run(conn: sqlite3.Connection, now: str, today: str, store_ref: str,
        errand_cards: list[dict] | None = None) -> dict:
    """Everything for a trip to one store.

    buy        open 'needed' items you can get there (handled ones excluded)
    handled    needed but already handled elsewhere (Subscribe & Save …)
    coming     items already ordered / arriving — don't buy twice
    running_low suggestions only
    cards      errand cards (BUY FIRST) whose store matches, passed in by the caller
    """
    store = resolve_store(conn, store_ref)
    if store is None:
        raise ValueError(f"unknown store {store_ref!r}")
    all_stores = {s["key"]: s for s in stores(conn)}
    profs = _profiles_by_id(conn)
    buy, handled, coming = [], [], []
    for it in items(conn, "open", now=now):
        if it["status"] == "done":
            continue
        p = profs.get(it["profile_id"]) if it["profile_id"] else None
        if not item_at_store(it, store, p):
            continue
        d = _decorate(it, p, store, all_stores)
        if it["status"] in ("ordered", "arriving"):
            coming.append(d)
        elif it["handled"]:
            handled.append(d)
        else:
            buy.append(d)
    buy.sort(key=lambda d: (CATEGORIES.index(d["category"]) if d.get("category") in CATEGORIES
                            else 99, d["position"]))
    total = round(sum(d["est_price"] or 0 for d in buy), 2)
    return {"store": store, "buy": buy, "handled": handled, "coming": coming,
            "running_low": running_low(conn, today, store), "cards": errand_cards or [],
            "est_total": total, "priced": sum(1 for d in buy if d.get("est_price") is not None)}


def overview(conn: sqlite3.Connection, now: str, today: str) -> dict:
    """The whole open list grouped by store (an item can sit under several),
    plus 'anywhere' for items no store takes yet."""
    all_stores = stores(conn)
    by_key = {s["key"]: s for s in all_stores}
    profs = _profiles_by_id(conn)
    groups = {s["key"]: [] for s in all_stores}
    anywhere, open_items = [], []
    for it in items(conn, "open", now=now):
        p = profs.get(it["profile_id"]) if it["profile_id"] else None
        d = _decorate(it, p, None, by_key)
        d["at"] = [s["key"] for s in all_stores if item_at_store(it, s, p)]
        open_items.append(d)
        if it["status"] != "needed":
            continue
        if not d["at"]:
            anywhere.append(d["id"])
        for k in d["at"]:
            groups[k].append(d["id"])
    needed = [d for d in open_items if d["status"] == "needed"]
    return {
        "items": open_items,
        "stores": all_stores,
        "groups": groups,
        "anywhere": anywhere,
        "counts": {s: sum(1 for d in open_items if d["status"] == s) for s in OPEN},
        "est_total": round(sum(d["est_price"] or 0 for d in needed if not d["handled"]), 2),
        "running_low": running_low(conn, today),
        "questions": open_questions(conn),
    }


# ------------------------------------------------------------- needed by
DUE_SOON_DAYS = 7


def _cat_rank(d: dict) -> int:
    return CATEGORIES.index(d["category"]) if d.get("category") in CATEGORIES else 99


def apply_needed_by(rows: list[dict], today: str) -> None:
    """Effective date = the item's own needed_by, else its card's due.  Adds
    ``needed_by_eff`` and ``due_in`` (days from today, negative = overdue)."""
    t = dt.date.fromisoformat(today)
    for d in rows:
        nb = d.get("needed_by") or ((d.get("card") or {}).get("due") or "")[:10] or None
        d["needed_by_eff"] = nb
        d["due_in"] = (dt.date.fromisoformat(nb) - t).days if nb else None


def sort_buy(rows: list[dict]) -> None:
    """Needed-by first (overdue, then soonest), then category order, then position."""
    rows.sort(key=lambda d: (0 if d.get("due_in") is not None else 1, d.get("due_in") or 0,
                             _cat_rank(d), d["position"]))


def due_soon(rows: list[dict]) -> list[dict]:
    return [d for d in rows if d["status"] == "needed" and not d.get("handled")
            and d.get("due_in") is not None and d["due_in"] <= DUE_SOON_DAYS]


def suggested_run(ov: dict, min_items: int = 4) -> dict | None:
    """The one in-store trip worth suggesting: the store that would clear the most
    needed items, if that is at least ``min_items`` or any of them is due soon."""
    by_id = {d["id"]: d for d in ov["items"]}
    best = None
    for s in ov["stores"]:
        if s["kind"] != "in_store":
            continue
        ids = [i for i in ov["groups"].get(s["key"], []) if not by_id[i].get("handled")]
        soon = [by_id[i] for i in ids if by_id[i].get("due_in") is not None
                and by_id[i]["due_in"] <= DUE_SOON_DAYS]
        if not ids or (len(ids) < min_items and not soon):
            continue
        score = (len(soon), len(ids))
        if best is None or score > best[0]:
            best = (score, s, ids, soon)
    if best is None:
        return None
    _, s, ids, soon = best
    return {"store": s["key"], "name": s["name"], "count": len(ids),
            "due": [{"name": by_id[i]["name"], "needed_by": by_id[i]["needed_by_eff"],
                     "due_in": by_id[i]["due_in"]} for i in [d["id"] for d in soon]],
            "overdue": any(d["due_in"] < 0 for d in soon)}


# ----------------------------------------------------------- order linking
def _order_tokens(order: dict) -> set[str]:
    return tokens(order.get("items"))


def _item_tokens(conn: sqlite3.Connection, it: dict, profs: dict) -> set[str]:
    words = [it["name"]]
    p = profs.get(it["profile_id"]) if it.get("profile_id") else None
    if p:
        words += [p["name"], *p["aliases"]]
        words += [x.get("brand") or "" for x in (p.get("prefs") or {}).values()]
    return tokens(" ".join(words))


def _score(item_toks: set[str], name_toks: set[str], order_toks: set[str]) -> float:
    """Share of the item's own name words found in the order text (brands/aliases help)."""
    if not order_toks or not name_toks:
        return 0.0
    hit = len(name_toks & order_toks) / len(name_toks)
    if order_toks <= name_toks and hit >= 0.5:
        hit = max(hit, 0.7)  # a short order line ("Flapper") inside the item's name
    bonus = 0.25 if (item_toks - name_toks) & order_toks else 0.0
    return min(1.0, hit + bonus)


def is_generic(items_text: str | None) -> bool:
    t = (items_text or "").strip()
    return not t or bool(_GENERIC_ORDER.match(t)) or t.lower().startswith("unknown item")


def _set_status_from_order(conn: sqlite3.Connection, now: str, it: dict, order: dict) -> bool:
    st = ORDER_STAGE_STATUS.get(order.get("stage"))
    if st is None:
        return False
    if st == "needed":  # cancelled: back on the list, unlinked
        conn.execute("UPDATE shop_items SET status='needed', order_key=NULL, done_at=NULL, "
                     "updated_at=? WHERE id = ?", (now, it["id"]))
        return True
    if it["status"] == st:
        return False
    if st == "done":
        s = resolve_store(conn, order.get("store"))
        record_purchase(conn, now, it, s["key"] if s else None, None, order["key"], "order",
                        order.get("delivered_at"))
    conn.execute("UPDATE shop_items SET status=?, order_key=?, done_at=?, updated_at=? WHERE id = ?",
                 (st, order["key"], now if st == "done" else None, now, it["id"]))
    return True


def parse_item_list(text: str | None) -> list[dict]:
    """'name | qty | price ; name | qty | price' (or one per line) -> dicts."""
    out = []
    for raw in re.split(r"\s*;\s*|\n+", text or ""):
        raw = raw.strip()
        if not raw:
            continue
        parts = [x.strip() for x in raw.split("|")]
        name = parts[0]
        if not name or len(name) < 2:
            continue
        qty = parts[1] if len(parts) > 1 and parts[1] else None
        price = _price(parts[2]) if len(parts) > 2 else None
        if price is None and qty is not None and _price(qty) is not None and ("$" in qty):
            price, qty = _price(qty), None  # "name | $4.99"
        out.append({"name": name[:120], "qty": qty, "price": price})
    return out


def _match_lines_to_items(conn: sqlite3.Connection, lines: list[dict], cands: list[dict],
                          profs: dict) -> dict[str, int]:
    """Greedy best-score pairing item id -> line index (each used once)."""
    pairs = []
    for it in cands:
        it_toks, name_toks = _item_tokens(conn, it, profs), tokens(it["name"])
        for i, ln in enumerate(lines):
            sc = _score(it_toks, name_toks, tokens(ln["name"]))
            if sc >= 0.6:
                pairs.append((sc, it["id"], i))
    pairs.sort(key=lambda x: -x[0])
    used_i, used_l, out = set(), set(), {}
    for sc, iid, i in pairs:
        if iid in used_i or i in used_l:
            continue
        used_i.add(iid); used_l.add(i); out[iid] = i
    return out


def _line_profile(conn: sqlite3.Connection, line_name: str, store_key: str | None,
                  profiles_cache: list[dict]) -> dict | None:
    """Which profile a receipt line is: exact name/alias first, then the best token
    match (ties: more name words matched, then a brand match at this store)."""
    p = get_profile(conn, line_name)
    if p:
        return p
    ltoks = tokens(line_name)
    best, best_key = None, (0.0, 0, 0)
    for p in profiles_cache:
        names = [p["name"], *p["aliases"]]
        for nm in names:
            nt = tokens(nm)
            if not nt:
                continue
            hit = len(nt & ltoks)
            sc = hit / len(nt)
            if sc < 0.75:
                continue
            brand = (p.get("prefs") or {}).get(store_key or "", {}).get("brand") or ""
            key = (sc, hit, len(tokens(brand) & ltoks))
            if key > best_key:
                best, best_key = p, key
    return best


def _apply_item_list(conn: sqlite3.Connection, now: str, order: dict, store: dict | None,
                     profs: dict) -> dict:
    """A multi-item (grocery) order: each receipt line moves a matching needed item
    through the order's stage, and on delivery every line becomes a purchase — for
    its list item when there is one, else for the profile it names.  That is what
    builds the restock cadence and keeps per-store prices current."""
    lines = parse_item_list(order.get("item_list"))
    res = {"matched": [], "purchases": 0}
    if not lines:
        return res
    cands = []
    for r in conn.execute("SELECT * FROM shop_items WHERE status IN ('needed','ordered','arriving') "
                          "AND (order_key IS NULL OR order_key = ?)", (order["key"],)):
        it = _item(r)
        p = profs.get(it["profile_id"]) if it["profile_id"] else None
        if store is not None and not item_at_store(it, store, p):
            continue
        if store is None and it["stores"]:
            continue
        cands.append(it)
    pairing = _match_lines_to_items(conn, lines, cands, profs)
    delivered = order.get("stage") == "delivered"
    already = delivered and conn.execute(
        "SELECT 1 FROM shop_purchases WHERE order_key = ? AND source = 'order_line' LIMIT 1",
        (order["key"],)).fetchone() is not None
    matched_lines = set()
    for it in cands:
        i = pairing.get(it["id"])
        if i is None:
            continue
        matched_lines.add(i)
        ln = lines[i]
        if delivered:
            if it["status"] != "done":
                conn.execute("UPDATE shop_items SET status='done', order_key=?, done_at=?, updated_at=? "
                             "WHERE id = ?", (order["key"], now, now, it["id"]))
                res["matched"].append(it["id"])
            if not already:
                record_purchase(conn, now, {**it, "qty": ln["qty"] or it.get("qty")},
                                store["key"] if store else None, ln["price"], order["key"],
                                "order_line", order.get("delivered_at"))
                res["purchases"] += 1
        else:
            st = ORDER_STAGE_STATUS.get(order.get("stage"))
            if st and st != "needed" and it["status"] != st:
                conn.execute("UPDATE shop_items SET status=?, order_key=?, updated_at=? WHERE id = ?",
                             (st, order["key"], now, it["id"]))
                res["matched"].append(it["id"])
    if delivered and not already:
        cache = profiles(conn)
        for i, ln in enumerate(lines):
            if i in matched_lines:
                continue
            p = _line_profile(conn, ln["name"], store["key"] if store else None, cache)
            record_purchase(conn, now, {"id": None, "profile_id": p["id"] if p else None,
                                        "name": p["name"] if p else ln["name"], "qty": ln["qty"]},
                            store["key"] if store else None, ln["price"], order["key"],
                            "order_line", order.get("delivered_at"))
            res["purchases"] += 1
    return res


def link_order(conn: sqlite3.Connection, now: str, order: dict) -> dict:
    """Called after every order upsert.  Moves linked items through their stages and links
    new ones.  Returns {changed: [ids], linked: [ids], question: {...}|None, card_uid}."""
    changed, linked = [], []
    lines_res = None
    if order.get("item_list") and order.get("stage") != "cancelled":
        # an itemized (grocery) order: the receipt lines decide first, with prices
        lines_res = _apply_item_list(conn, now, order, resolve_store(conn, order.get("store")),
                                     _profiles_by_id(conn))
        changed += lines_res["matched"]
    for r in conn.execute("SELECT * FROM shop_items WHERE order_key = ? AND status != 'dismissed'",
                          (order["key"],)).fetchall():
        it = _item(r)
        if _set_status_from_order(conn, now, it, order) and it["id"] not in changed:
            changed.append(it["id"])
    question = None
    already = conn.execute("SELECT 1 FROM shop_items WHERE order_key = ? LIMIT 1",
                           (order["key"],)).fetchone()
    if order.get("stage") != "cancelled" and not already:
        store = resolve_store(conn, order.get("store"))
        profs = _profiles_by_id(conn)
        cands = []
        for r in conn.execute("SELECT * FROM shop_items WHERE status = 'needed'"):
            it = _item(r)
            p = profs.get(it["profile_id"]) if it["profile_id"] else None
            if store is not None and not item_at_store(it, store, p):
                continue
            if store is None and it["stores"]:
                continue
            cands.append(it)
        otoks = _order_tokens(order)
        scored = []
        for it in cands:
            s = _score(_item_tokens(conn, it, profs), tokens(it["name"]), otoks)
            if s >= 0.6:
                scored.append((s, it))
        if scored:
            for _s, it in sorted(scored, key=lambda x: -x[0]):
                if _set_status_from_order(conn, now, it, order):
                    linked.append(it["id"])
        elif is_generic(order.get("items")) and cands and order.get("stage") in (
                "ordered", "shipped", "out_for_delivery") and not conn.execute(
                "SELECT 1 FROM shop_questions WHERE order_key = ?", (order["key"],)).fetchone():
            hint = tokens(order.get("items")) - {"item"}
            ranked = sorted(cands, key=lambda it: (
                -(1 if (it.get("category") or "") in hint else 0), it["position"]))[:4]
            question = {"id": uuid.uuid4().hex[:10], "order_key": order["key"],
                        "candidates": [{"id": i["id"], "name": i["name"]} for i in ranked]}
            conn.execute(
                "INSERT INTO shop_questions(id, order_key, candidates, created_at) VALUES (?,?,?,?)",
                (question["id"], order["key"], json.dumps(question["candidates"]), now))
        if not scored and question is None and not is_generic(order.get("items")) and \
                order.get("stage") in ("ordered", "shipped", "out_for_delivery"):
            # Active delivery nobody listed: track it anyway so store runs know it is
            # coming, and its delivery becomes purchase history (cadence, price).
            iid = created = uuid.uuid4().hex[:12]
            name = re.sub(r"\s+", " ", order["items"]).strip()[:90]
            prof = get_profile(conn, name)
            pos = conn.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM shop_items").fetchone()[0]
            conn.execute(
                "INSERT INTO shop_items(id,name,qty,note,category,stores,profile_id,card_uid,status,"
                "order_key,handled,added_by,source,position,created_at,updated_at,done_at) "
                "VALUES (?,?,NULL,NULL,?,?,?,?,?,?,NULL,NULL,'order',?,?,?,NULL)",
                (iid, name, (prof or {}).get("category") or guess_category(name),
                 json.dumps([store["key"]] if store else []), (prof or {}).get("id"),
                 order.get("card_uid"), ORDER_STAGE_STATUS[order["stage"]], order["key"], pos, now, now))
            linked.append(created)
    card = None
    if not order.get("card_uid"):
        cards = {r["card_uid"] for r in conn.execute(
            "SELECT card_uid FROM shop_items WHERE order_key = ? AND card_uid IS NOT NULL",
            (order["key"],))}
        if len(cards) == 1:
            card = cards.pop()
    return {"changed": changed, "linked": linked, "question": question, "card_uid": card,
            "lines": lines_res}


def open_questions(conn: sqlite3.Connection) -> list[dict]:
    out = []
    for r in conn.execute("SELECT q.*, o.store, o.items, o.total, o.order_number FROM shop_questions q "
                          "LEFT JOIN orders o ON o.key = q.order_key WHERE q.answered_at IS NULL "
                          "ORDER BY q.created_at"):
        out.append({"id": r["id"], "order_key": r["order_key"], "store": r["store"],
                    "order_items": r["items"], "total": r["total"],
                    "order_number": r["order_number"], "candidates": _j(r["candidates"])})
    return out


def answer_question(conn: sqlite3.Connection, now: str, qid: str, item_ref: str | None,
                    order: dict | None) -> dict:
    """item_ref None = 'none of these'.  Links the item and catches it up to the order's stage."""
    q = conn.execute("SELECT * FROM shop_questions WHERE id = ?", (qid,)).fetchone()
    if q is None:
        raise ValueError(f"no question {qid}")
    res = {"question": qid, "linked": None}
    if item_ref:
        it = get_item(conn, item_ref)
        if it is None:
            raise ValueError(f"no shopping item matches {item_ref!r}")
        if order is not None:
            _set_status_from_order(conn, now, it, order)
            res["linked"] = it["id"]
    conn.execute("UPDATE shop_questions SET answered_at = ?, answer = ? WHERE id = ?",
                 (now, item_ref or "none", qid))
    return res


def link_item_order(conn: sqlite3.Connection, now: str, item_ref: str, order: dict) -> dict:
    it = get_item(conn, item_ref)
    if it is None:
        raise ValueError(f"no shopping item matches {item_ref!r}")
    _set_status_from_order(conn, now, it, order)
    conn.execute("UPDATE shop_questions SET answered_at = ?, answer = ? WHERE order_key = ? "
                 "AND answered_at IS NULL", (now, it["id"], order["key"]))
    return get_item(conn, it["id"])


# ------------------------------------------------------- repeating cards
def restock_from_cards(conn: sqlite3.Connection, now: str, today: str,
                       cards: dict[str, dict]) -> list[str]:
    """A bought item tied to a REPEAT card comes back when the card's next cycle nears.

    ``cards``: uid -> {status, due, repeat, last_cycle} where last_cycle is the date
    of the card's newest "done; next due" note (or None).  An item that was done
    BEFORE that cycle started and whose card is due within RESTOCK_LEAD_DAYS goes
    back to 'needed'.
    """
    t = dt.date.fromisoformat(today)
    lead = (t + dt.timedelta(days=RESTOCK_LEAD_DAYS)).isoformat()
    back = []
    for r in conn.execute("SELECT * FROM shop_items WHERE status = 'done' AND card_uid IS NOT NULL"):
        c = cards.get(r["card_uid"])
        if not c or c.get("status") != "needs_action" or not c.get("repeat") or not c.get("due"):
            continue
        if c["due"][:10] > lead or not c.get("last_cycle"):
            continue
        if (r["done_at"] or "")[:10] <= c["last_cycle"]:
            conn.execute("UPDATE shop_items SET status='needed', order_key=NULL, done_at=NULL, "
                         "note=?, updated_at=? WHERE id = ?",
                         (f"restock for {c['due'][:10]}", now, r["id"]))
            back.append(r["id"])
    return back


# ---------------------------------------------------------------- export
def export(conn: sqlite3.Connection) -> dict:
    out = {}
    for t in ("shop_stores", "shop_profiles", "shop_prefs", "shop_purchases", "shop_items",
              "shop_questions"):
        out[t] = [dict(r) for r in conn.execute(f"SELECT * FROM {t}")]  # noqa: S608
    return out


def sync_orders(conn: sqlite3.Connection, now: str, link=None) -> dict:
    """Run link_order over every active order (backfill / repair).  ``link`` lets the
    store wrap it so card hand-off happens the same way as on upsert."""
    out = {"orders": 0, "linked": [], "questions": []}
    keys = [r["key"] for r in conn.execute(
        "SELECT key FROM orders WHERE stage IN ('ordered','shipped','out_for_delivery') "
        "ORDER BY first_seen")]
    from . import orders as orders_mod  # noqa: PLC0415
    for k in keys:
        o = orders_mod.find(conn, k)
        res = (link or link_order)(conn, now, o)
        out["orders"] += 1
        out["linked"] += res["linked"]
        if res["question"]:
            out["questions"].append(res["question"])
    return out
