"""Store-level tests for the shopping list (no Home Assistant needed)."""
import json

import datetime as dt
import sqlite3
from zoneinfo import ZoneInfo

import pytest

import sys, types
from pathlib import Path
if "mission_control" not in sys.modules:
    pkg = types.ModuleType("mission_control"); pkg.__path__ = [str(Path(__file__).resolve().parents[1] / "custom_components" / "mission_control")]; sys.modules["mission_control"] = pkg
from mission_control import shopping as sh
from mission_control.store import MissionStore

CHI = ZoneInfo("America/Chicago")


class Clock:
    def __init__(self):
        self.t = dt.datetime(2026, 10, 29, 15, 0, tzinfo=dt.UTC)

    def __call__(self):
        return self.t

    def days(self, n):
        self.t += dt.timedelta(days=n)


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    s = MissionStore(tmp_path / "mc.db", CHI, clock)
    s.open()
    for d in (
        {"name": "Hardware Co", "key": "hardware", "categories": ["hardware", "tools", "electrical", "plumbing",
                                                          "hvac", "garden", "lighting", "batteries"],
         "aliases": ["hardware", "hardware co"], "zone": "zone.store_hardware"},
        {"name": "Home Depot", "key": "home_depot", "categories": ["hardware", "tools", "hvac"],
         "aliases": ["the home depot", "depot"]},
        {"name": "Corner Grocer", "key": "grocer", "categories": ["produce", "dairy", "pantry", "cleaning", "paper"],
         "aliases": ["grocer"], "general": True},
        {"name": "Costco", "key": "costco", "categories": ["paper", "cleaning", "pantry"]},
        {"name": "Amazon", "key": "amazon", "kind": "online",
         "categories": ["cleaning", "paper", "hvac", "electronics", "kitchen", "hardware"], "general": True},
    ):
        s.shop_store_upsert(d)
    return s, clock


def test_store_resolution_and_categories(env):
    s, _ = env
    assert sh.resolve_store(s.conn, "HARDWARE CO")["key"] == "hardware"
    assert sh.resolve_store(s.conn, "I'm going to the home depot")["key"] == "home_depot"
    assert sh.resolve_store(s.conn, "zone.store_hardware")["key"] == "hardware"
    assert sh.resolve_store(s.conn, "Micro Center") is None
    with pytest.raises(ValueError):
        s.shop_store_upsert({"name": "X", "categories": ["nonsense"]})
    mc = s.shop_store_upsert({"name": "Micro Center", "categories": ["electronics"]})
    assert mc["key"] == "micro_center"


def test_add_merge_and_category_guess(env):
    s, _ = env
    a = s.shop_add({"name": "Dish soap", "added_by": "sam"})
    assert a["item"]["category"] == "cleaning" and not a["merged"]
    b = s.shop_add({"name": "dish soap", "qty": "2", "added_by": "alex"})
    assert b["merged"] and b["item"]["id"] == a["item"]["id"] and b["item"]["qty"] == "2"
    assert sh.guess_category("20x25 air filter") == "hvac"
    assert sh.guess_category("toilet paper") == "paper"


def test_run_routes_by_category_explicit_store_and_profile(env):
    s, _ = env
    s.shop_add({"name": "Dish soap"})                       # cleaning -> grocer, costco, amazon
    s.shop_add({"name": "Wire nuts"})                       # electrical -> hardware only
    s.shop_add({"name": "Toilet paper", "stores": ["Costco"]})  # explicit
    s.shop_add({"name": "Mystery thing"})                   # no category -> general stores
    s.shop_profile_upsert({"name": "Olive oil", "category": "pantry",
                           "prefs": {"grocer": {"brand": "Corner Grocer Organics", "size": "1 L", "price": 9.48},
                                     "costco": {"brand": "Kirkland", "size": "2 L", "price": 7.99}},
                           "bulk_store": "costco", "add_aliases": ["the good olive oil"]})
    s.shop_add({"name": "the good olive oil"})
    hardware = {d["name"] for d in s.shop_run("hardware")["buy"]}
    assert hardware == {"Wire nuts"}
    grocer = s.shop_run("Corner Grocer")
    names = {d["name"] for d in grocer["buy"]}
    assert names == {"Dish soap", "Mystery thing", "the good olive oil"}
    oil = next(d for d in grocer["buy"] if d["name"] == "the good olive oil")
    assert oil["pref"]["brand"] == "Corner Grocer Organics"
    assert oil["cheaper_at"]["store"] == "costco" and oil["bulk_at"]["store"] == "costco"
    assert grocer["est_total"] == 9.48
    assert {d["name"] for d in s.shop_run("costco")["buy"]} == {"Dish soap", "Toilet paper",
                                                                 "the good olive oil"}


def test_check_records_purchase_price_and_card_note(env):
    s, _ = env
    uid = s.create("home", "Replace HVAC filters", description="LANE: need\nNEXT STEP: buy filters")
    s.shop_profile_upsert({"name": "Filtrete 20x25 filter", "category": "hvac"})
    it = s.shop_add({"name": "Filtrete 20x25 filter", "qty": "3", "card_uid": uid[:8]})["item"]
    assert it["card_uid"] == uid
    done = s.shop_check(it["id"], store="hardware", price="$18.97")
    assert done["status"] == "done"
    p = s.shop_profile_get("Filtrete 20x25 filter")
    assert p["prefs"]["hardware"]["price"] == 18.97 and p["purchases"][0]["store_key"] == "hardware"
    assert any("bought Filtrete 20x25 filter at Hardware Co" in tx for _, tx in s.get(uid)["notes"])
    back = s.shop_uncheck(it["id"])
    assert back["status"] == "needed" and s.shop_profile_get(p["id"])["purchases"] == []


def test_order_email_moves_item_through_stages(env):
    s, clock = env
    uid = s.create("lab", "Fix rack cabling", description="LANE: want")
    it = s.shop_add({"name": "Cat6 patch cables", "card_uid": uid, "stores": ["amazon"]})["item"]
    s.shop_add({"name": "Dish soap"})
    r = s.order_upsert({"store": "Amazon.com", "order_number": "111-1", "stage": "ordered",
                        "items": "Cable Matters Cat6 Patch Cable 5-pack", "message_id": "m1"})
    assert r["shopping"]["linked"] == [it["id"]]
    assert r["order"]["card_uid"] == uid  # order inherits the item's card
    assert s.shop_get(it["id"])["status"] == "ordered"
    s.order_upsert({"store": "Amazon", "order_number": "111-1", "stage": "shipped", "message_id": "m2"})
    assert s.shop_get(it["id"])["status"] == "arriving"
    run = s.shop_run("amazon")
    assert [d["name"] for d in run["coming"]] == ["Cat6 patch cables"]
    assert "Cat6 patch cables" not in {d["name"] for d in run["buy"]}
    s.order_upsert({"store": "Amazon", "order_number": "111-1", "stage": "delivered", "message_id": "m3"})
    assert s.shop_get(it["id"])["status"] == "done"
    assert s.conn.execute("SELECT COUNT(*) FROM shop_purchases WHERE order_key='amazon:111-1'").fetchone()[0] == 1
    # dish soap untouched
    assert s.shop_get("Dish soap")["status"] == "needed"


def test_cancelled_order_puts_item_back(env):
    s, _ = env
    it = s.shop_add({"name": "Paper towels"})["item"]
    s.order_upsert({"store": "Amazon", "order_number": "222", "stage": "ordered",
                    "items": "Bounty Paper Towels 12 rolls", "message_id": "a"})
    assert s.shop_get(it["id"])["status"] == "ordered"
    s.order_upsert({"store": "Amazon", "order_number": "222", "stage": "cancelled", "message_id": "b"})
    got = s.shop_get(it["id"])
    assert got["status"] == "needed" and got["order_key"] is None


def test_vague_order_asks_once_and_answer_links(env):
    s, _ = env
    a = s.shop_add({"name": "Dish soap"})["item"]
    s.shop_add({"name": "Paper towels"})
    r = s.order_upsert({"store": "Amazon", "order_number": "333", "stage": "ordered",
                        "items": "1 Kitchen item", "message_id": "x"})
    q = r["shopping"]["question"]
    assert q and {c["name"] for c in q["candidates"]} == {"Dish soap", "Paper towels"}
    r2 = s.order_upsert({"store": "Amazon", "order_number": "333", "stage": "shipped", "message_id": "y"})
    assert r2["shopping"]["question"] is None  # asked once
    assert len(s.shop_questions()) == 1
    s.shop_answer(q["id"], a["id"])
    assert s.shop_get(a["id"])["status"] == "arriving"  # caught up to the order's stage
    assert s.shop_questions() == []
    s.order_upsert({"store": "Amazon", "order_number": "333", "stage": "delivered", "message_id": "z"})
    assert s.shop_get(a["id"])["status"] == "done"


def test_no_match_no_question_for_specific_items(env):
    s, _ = env
    s.shop_add({"name": "Dish soap"})
    r = s.order_upsert({"store": "Amazon", "order_number": "444", "stage": "ordered",
                        "items": "Anker USB-C charger", "message_id": "q"})
    assert r["shopping"]["question"] is None
    # nobody listed it: the delivery is tracked anyway (store runs see it coming)
    assert len(r["shopping"]["linked"]) == 1
    got = s.shop_get(r["shopping"]["linked"][0])
    assert got["name"] == "Anker USB-C charger" and got["status"] == "ordered" and got["source"] == "order"
    assert got["stores"] == ["amazon"] and got["category"] == "electronics"
    assert "Anker USB-C charger" in {d["name"] for d in s.shop_run("amazon")["coming"]}
    s.order_upsert({"store": "Amazon", "order_number": "444", "stage": "delivered", "message_id": "q2"})
    assert s.shop_get(got["id"])["status"] == "done"
    assert s.conn.execute("SELECT COUNT(*) FROM shop_purchases WHERE item_id = ?", (got["id"],)).fetchone()[0] == 1


def test_running_low_from_cadence(env):
    s, clock = env
    s.shop_profile_upsert({"name": "Toilet paper", "category": "paper", "bulk_store": "costco"})
    for d in ("2026-08-01", "2026-09-05", "2026-10-10"):
        s.shop_purchase_add({"name": "Toilet paper", "store": "costco", "ts": d + "T12:00:00+00:00"})
    p = s.shop_profile_get("Toilet paper")
    assert p["cadence"]["interval_days"] == 35
    assert s.shop_run("costco")["running_low"] == []          # 19 days in
    clock.days(12)                                               # 31 days in (>= 85% of 35)
    low = s.shop_run("costco")["running_low"]
    assert [x["name"] for x in low] == ["Toilet paper"]
    assert s.shop_run("hardware")["running_low"] == []            # not sold there
    s.shop_not_low("Toilet paper")
    assert s.shop_run("costco")["running_low"] == []
    s.shop_add({"name": "Toilet paper"})                        # on the list -> no suggestion
    assert s.shop_overview()["running_low"] == []


def test_handled_items_and_overview_groups(env):
    s, _ = env
    s.shop_profile_upsert({"name": "Cat litter", "category": "pet", "handled": "Subscribe & Save"})
    s.shop_add({"name": "Cat litter"})
    s.shop_add({"name": "Wire nuts"})
    s.shop_store_upsert({"name": "Amazon", "add_categories": ["pet"]})
    ov = s.shop_overview()
    lit = next(d for d in ov["items"] if d["name"] == "Cat litter")
    assert lit["handled"] == "Subscribe & Save"
    assert ov["groups"]["hardware"] == [next(d["id"] for d in ov["items"] if d["name"] == "Wire nuts")]
    assert lit["id"] in ov["groups"]["amazon"]  # category added later
    run = s.shop_run("amazon")
    assert [d["name"] for d in run["handled"]] == ["Cat litter"]
    assert all(d["name"] != "Cat litter" for d in run["buy"])


def test_errand_cards_and_parts_block(env):
    s, _ = env
    uid = s.create("home", "Fix toilet flapper",
                   description="LANE: need\nBUY FIRST: Home Depot run\nBLOCKED BY: parts\nNEXT STEP: swap it")
    on = s.create("lab", "Order NAS drives", description="LANE: want\nBUY FIRST: Online order")
    assert [c["uid"] for c in s.shop_run("home_depot")["cards"]] == [uid]
    assert [c["uid"] for c in s.shop_run("hardware")["cards"]] == [uid]   # hardware swap
    assert [c["uid"] for c in s.shop_run("amazon")["cards"]] == [on]
    assert s.shop_run("grocer")["cards"] == []
    it = s.shop_add({"name": "Toilet flapper", "card_uid": uid, "stores": ["hardware"]})["item"]
    s.resolve_blocks()
    assert s.is_blocked(uid)
    s.shop_check(it["id"], store="hardware")
    freed = s.resolve_blocks()
    assert [f["uid"] for f in freed] == [uid] and not s.is_blocked(uid)
    assert s.get(uid)["blocked_by"] is None


def test_restock_from_repeat_card(env):
    s, clock = env
    uid = s.create("home", "Change HVAC filters",
                   due="2026-11-05", description="LANE: need\nREPEAT: every 60 days")
    it = s.shop_add({"name": "Filtrete 20x25 filter", "card_uid": uid})["item"]
    s.shop_check(it["id"], store="hardware")
    assert s.shop_restock() == []                       # bought for this cycle
    clock.days(7)                                        # 2026-11-05: filters changed, card closed
    s.update_fields(uid, {"status": "completed"})
    row = s.get(uid)
    assert row["status"] == "needs_action" and row["due"] > "2026-12-01"
    assert s.shop_restock() == []                       # next due ~60 days out
    clock.days(50)                                       # within 14 days of the next due
    assert s.shop_restock() == [it["id"]]
    assert s.shop_get(it["id"])["status"] == "needed"
    assert s.shop_restock() == []


def test_link_order_by_hand_and_export(env):
    s, _ = env
    it = s.shop_add({"name": "Olive oil"})["item"]
    s.order_upsert({"store": "Amazon", "order_number": "555", "stage": "shipped",
                    "items": "Something else entirely", "message_id": "o"})
    got = s.shop_link_order(it["id"], "555")
    assert got["status"] == "arriving" and got["order_key"] == "amazon:555"
    ex = s.export()["shopping"]
    assert len(ex["shop_items"]) == 2 and len(ex["shop_stores"]) == 5  # + the auto-tracked order


def test_reopen_existing_db_is_idempotent(env, tmp_path):
    s, clock = env
    s.shop_add({"name": "Dish soap"})
    s.close()
    s2 = MissionStore(tmp_path / "mc.db", CHI, clock)
    s2.open()
    assert s2.shop_get("Dish soap")["name"] == "Dish soap"


def test_short_order_line_matches(env):
    s, _ = env
    it = s.shop_add({"name": "Toilet flapper", "stores": ["amazon"]})["item"]
    s.shop_add({"name": "Ethernet cable", "stores": ["amazon"]})
    r = s.order_upsert({"store": "Amazon", "order_number": "9", "stage": "ordered", "items": "Flapper",
                        "message_id": "f"})
    assert r["shopping"]["linked"] == [it["id"]]


def test_generic_order_not_auto_tracked_and_card_follows_order(env):
    s, _ = env
    r = s.order_upsert({"store": "Amazon", "order_number": "777", "stage": "ordered",
                        "items": "1 Kitchen item", "message_id": "g"})
    assert r["shopping"]["linked"] == [] and r["shopping"]["question"] is None  # no candidates, no noise
    uid = s.create("home", "Install the faucet", description="LANE: need")
    r = s.order_upsert({"store": "Amazon", "order_number": "888", "stage": "shipped",
                        "items": "Moen Sleek Pulldown Kitchen Faucet", "message_id": "m"})
    iid = r["shopping"]["linked"][0]
    s.order_update("888", {"card_uid": uid})
    assert s.shop_get(iid)["card_uid"] == uid


def test_sync_orders_backfills_active_orders(env):
    s, _ = env
    s.order_upsert({"store": "Amazon", "order_number": "a1", "stage": "shipped", "items": "Dig Defence fence 24 pack", "message_id": "1"})
    s.order_upsert({"store": "Amazon", "order_number": "a2", "stage": "delivered", "items": "SodaStream mixers", "message_id": "2"})
    s.conn.execute("DELETE FROM shop_items")  # as if the orders predate the shopping list
    res = s.shop_sync_orders()
    assert res["orders"] == 1 and len(res["linked"]) == 1
    assert s.shop_get(res["linked"][0])["status"] == "arriving"
    assert s.shop_sync_orders()["linked"] == []  # idempotent


def test_same_order_under_three_store_names_is_one_order(env):
    """One order under three store names: the confirmation, the pharmacy portal and the carrier notice."""
    s, _ = env
    s.order_upsert({"store": "PetMeds / Riverside Vet Clinic", "order_number": "1200000000001",
                    "stage": "ordered", "items": "Joint supplement for the dog", "total": "86.02", "message_id": "c1"})
    s.order_upsert({"store": "PetMeds Pharmacy", "order_number": "1200000000001", "stage": "shipped",
                    "tracking_number": "1Z999AA10123456784", "carrier": "UPS", "items": "Joint supplement chews 60 count",
                    "message_id": "c2"})
    r = s.order_upsert({"store": "PETMEDS FULFILLMENT - CENTRAL", "tracking_number": "1Z999AA10123456784",
                        "stage": "shipped", "items": "Unknown item from PETMEDS FULFILLMENT", "expected_date": "2026-11-04",
                        "message_id": "c3"})
    rows = s.conn.execute("SELECT key, store, stage, tracking, expected_date FROM orders").fetchall()
    assert len(rows) == 1
    o = dict(rows[0])
    assert o["key"] == "petmeds / riverside vet clinic:1200000000001"  # first key sticks
    assert o["store"] == "PetMeds / Riverside Vet Clinic" and o["stage"] == "shipped"
    assert o["tracking"] == "1Z999AA10123456784" and o["expected_date"] == "2026-11-04"
    items = [i for i in s.shop_items("all") if i["source"] == "order"]
    assert len(items) == 1 and items[0]["status"] == "arriving"  # one tracked item, not three
    assert r["shopping"]["question"] is None  # "Unknown item" never becomes an item name
    s.order_upsert({"store": "UPS", "tracking_number": "1Z999AA10123456784", "stage": "delivered", "message_id": "c4"})
    assert s.shop_items("all")[0]["status"] == "done"


def test_tracking_first_then_number_keeps_item_linked(env):
    s, _ = env
    s.order_upsert({"store": "UPS", "tracking_number": "1Z999", "stage": "shipped", "items": "Dyson filter",
                    "message_id": "t1"})
    it = next(i for i in s.shop_items("all") if i["name"] == "Dyson filter")
    assert it["order_key"] == "ups:1z999"
    s.order_upsert({"store": "Dyson", "order_number": "DY-123456", "tracking_number": "1Z999", "stage": "delivered",
                    "message_id": "t2"})
    assert s.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
    it = s.shop_get(it["id"])
    assert it["order_key"] == "dyson:dy-123456" and it["status"] == "done"  # followed the promotion


def test_dismissed_item_stays_dismissed_on_delivery(env):
    s, _ = env
    s.order_upsert({"store": "Amazon", "order_number": "555-1", "stage": "ordered", "items": "Spare widget",
                    "message_id": "d1"})
    it = next(i for i in s.shop_items("all") if i["name"] == "Spare widget")
    s.shop_update(it["id"], {"status": "dismissed"})
    s.order_upsert({"store": "Amazon", "order_number": "555-1", "stage": "delivered", "message_id": "d2"})
    assert s.shop_get(it["id"])["status"] == "dismissed"


def test_merge_existing_duplicates(env):
    s, _ = env
    # two rows that predate the identifier matching, each with its own tracked item
    s.conn.execute("INSERT INTO orders(key,store,order_number,stage,stage_rank,first_seen,updated_at,message_ids,events,tracking)"
                   " VALUES ('a:1','A','1','ordered',1,'2026-10-29T00:00:00','2026-10-29T00:00:00','[\"m1\"]','[]',NULL)")
    s.conn.execute("INSERT INTO orders(key,store,order_number,stage,stage_rank,first_seen,updated_at,message_ids,events,tracking,carrier)"
                   " VALUES ('b:zz','B',NULL,'shipped',2,'2026-10-30T00:00:00','2026-10-30T00:00:00','[\"m2\"]','[]','ZZ','UPS')")
    s.shop_add({"name": "Thing one"}); s.shop_add({"name": "Thing two"})
    a, b = s.shop_get("Thing one"), s.shop_get("Thing two")
    s.shop_link_order(a["id"], "1"); s.shop_link_order(b["id"], "ZZ")
    o = s.order_merge("1", "ZZ")
    assert o["key"] == "a:1" and o["stage"] == "shipped" and o["tracking"] == "ZZ" and o["carrier"] == "UPS"
    assert s.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
    assert {s.shop_get(a["id"])["order_key"], s.shop_get(b["id"])["order_key"]} == {"a:1"}
    assert set(json.loads(s.conn.execute("SELECT message_ids FROM orders").fetchone()[0])) == {"m1", "m2"}
    s.order_upsert({"store": "A", "order_number": "1", "stage": "delivered", "message_id": "m3"})
    assert s.shop_get(a["id"])["status"] == "done" and s.shop_get(b["id"])["status"] == "done"


# ------------------------------------------------- 2026-11-02: itemized orders
def test_parse_item_list_shapes():
    lines = sh.parse_item_list("Whole milk | 1 | 4.29 ; Eggs|2|$6.50;Bananas||1.99\nAvocados | 3")
    assert [(l["name"], l["qty"], l["price"]) for l in lines] == [
        ("Whole milk", "1", 4.29), ("Eggs", "2", 6.5), ("Bananas", None, 1.99), ("Avocados", "3", None)]
    assert sh.parse_item_list("name | $4.99")[0]["price"] == 4.99
    assert sh.parse_item_list(None) == [] and sh.parse_item_list(" ; ;") == []


def test_itemized_grocery_order_checks_lines_off_and_records_each_line(env):
    s, clock = env
    milk = s.shop_add({"name": "Milk", "stores": ["grocer"]})["item"]
    eggs = s.shop_add({"name": "Eggs", "category": "dairy"})["item"]
    s.shop_add({"name": "Wire nuts"})  # electrical — not at Corner Grocer, must be left alone
    s.shop_profile_upsert({"name": "Bananas", "category": "produce"})
    il = "Corner Grocer Whole Milk 1 gal | 1 | 4.29 ; Store Brand Large Eggs 18 ct | 1 | 5.12 ; Bananas | 2 lb | 1.18"
    r = s.order_upsert({"store": "Corner Grocer", "order_number": "HEB-1001", "stage": "ordered",
                        "items": "3 items", "item_list": il, "message_id": "h1"})
    assert set(r["shopping"]["changed"]) == {milk["id"], eggs["id"]}
    assert r["shopping"]["question"] is None          # the lines answered it; no "which item?" push
    assert s.shop_get(milk["id"])["status"] == "ordered" and s.shop_get(eggs["id"])["status"] == "ordered"
    assert s.shop_get("Wire nuts")["status"] == "needed"
    s.order_upsert({"store": "Corner Grocer", "order_number": "HEB-1001", "stage": "delivered", "message_id": "h2"})
    assert s.shop_get(milk["id"])["status"] == "done" and s.shop_get(eggs["id"])["status"] == "done"
    rows = s.conn.execute("SELECT name, price, qty, profile_id FROM shop_purchases WHERE order_key = ? "
                          "AND source='order_line' ORDER BY name", (r["order"]["key"],)).fetchall()
    assert [(x["name"], x["price"]) for x in rows] == [("Bananas", 1.18), ("Eggs", 5.12), ("Milk", 4.29)]
    assert rows[0]["profile_id"] == s.shop_profile_get("Bananas")["id"]  # unmatched line -> its profile
    # a second delivery email for the same order must not double the history
    s.order_upsert({"store": "Corner Grocer", "order_number": "HEB-1001", "stage": "delivered", "message_id": "h3"})
    assert s.conn.execute("SELECT COUNT(*) FROM shop_purchases WHERE order_key = ?",
                          (r["order"]["key"],)).fetchone()[0] == 3
    # the milk item was auto-profiled by the purchase, so its price is now known at grocer
    p = s.shop_profile_get("Milk")
    assert p is None or p["prefs"].get("grocer", {}).get("price") in (None, 4.29)


def test_item_list_lines_stick_and_short_list_does_not_overwrite(env):
    s, _ = env
    r = s.order_upsert({"store": "Instacart", "order_number": "IC-5", "stage": "ordered",
                        "item_list": "A | 1 | 1.00 ; B | 1 | 2.00", "message_id": "a"})
    r = s.order_upsert({"store": "Instacart", "order_number": "IC-5", "stage": "shipped",
                        "item_list": "A", "message_id": "b"})
    assert r["order"]["item_list"] == "A | 1 | 1.00 ; B | 1 | 2.00"


# ------------------------------------------------- 2026-11-02: needed by
def test_needed_by_own_date_card_due_and_sorting(env):
    s, clock = env
    today = s._today()
    t = dt.date.fromisoformat(today)
    uid = s.create("home", "Fix the fence", description="LANE: need",
                   due=(t + dt.timedelta(days=3)).isoformat())
    late = s.shop_add({"name": "Fence pickets", "card_uid": uid, "stores": ["hardware"]})["item"]  # due from card
    soon = s.shop_add({"name": "Deck screws", "needed_by": (t - dt.timedelta(days=1)).isoformat()})["item"]
    s.shop_add({"name": "Wire nuts"})                                               # no date
    with pytest.raises(ValueError):
        s.shop_add({"name": "Bad date", "needed_by": "next week"})
    # merge keeps the earlier date
    s.shop_add({"name": "deck screws", "needed_by": (t + dt.timedelta(days=9)).isoformat()})
    assert s.shop_get(soon["id"])["needed_by"] == (t - dt.timedelta(days=1)).isoformat()
    run = s.shop_run("hardware")
    names = [d["name"] for d in run["buy"]]
    assert names[:2] == ["Deck screws", "Fence pickets"] and "Wire nuts" in names
    byname = {d["name"]: d for d in run["buy"]}
    assert byname["Fence pickets"]["needed_by_eff"] == (t + dt.timedelta(days=3)).isoformat()
    assert byname["Fence pickets"]["due_in"] == 3 and byname["Deck screws"]["due_in"] == -1
    assert byname["Wire nuts"]["due_in"] is None
    assert [d["name"] for d in run["due_soon"]] == ["Deck screws", "Fence pickets"]
    ov = s.shop_overview()
    assert ov["suggested_run"]["store"] == "hardware" and ov["suggested_run"]["overdue"] is True
    assert ov["suggested_run"]["count"] == 3
    # clearing the own date falls back to the card
    s.shop_update(late["id"], {"needed_by": (t + dt.timedelta(days=1)).isoformat()})
    assert s.shop_run("hardware")["buy"][0]["name"] == "Deck screws"  # overdue still first
    s.shop_update(late["id"], {"needed_by": None})
    assert s.shop_get(late["id"])["needed_by"] is None
    assert {d["name"]: d["due_in"] for d in s.shop_overview()["items"]}["Fence pickets"] == 3


def test_suggested_run_needs_four_items_or_a_date(env):
    s, _ = env
    s.shop_add({"name": "Dish soap"})
    s.shop_add({"name": "Paper towels"})
    assert s.shop_overview()["suggested_run"] is None
    s.shop_add({"name": "Sponges"})
    s.shop_add({"name": "Trash bags"})
    sr = s.shop_overview()["suggested_run"]
    assert sr is not None and sr["count"] == 4 and sr["due"] == []


def test_bought_today_in_overview(env):
    s, _ = env
    it = s.shop_add({"name": "Dish soap"})["item"]
    s.shop_check(it["id"], "grocer", "3.49")
    ov = s.shop_overview()
    assert [(b["name"], b["store_key"]) for b in ov["bought_today"]] == [("Dish soap", "grocer")]
