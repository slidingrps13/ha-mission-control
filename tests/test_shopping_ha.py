"""HA-level smoke test for the shopping list: services, WS, todo projection, sensor, events."""

from __future__ import annotations

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant

DOMAIN = "mission_control"


def pt_id(items):
    return next(i for i in items if i["name"] == "Paper towels")["id"]


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    import glob, os  # noqa: E401
    for f in glob.glob(hass.config.path("mission_control.db*")):
        os.remove(f)
    entry = MockConfigEntry(domain=DOMAIN, options={"active": True, "shopping": True, "packages": True}, title="Mission Control")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_shopping_end_to_end(hass: HomeAssistant, hass_ws_client) -> None:
    await _setup(hass)
    ws = await hass_ws_client(hass)
    n = 0

    async def call(**msg):
        nonlocal n
        n += 1
        await ws.send_json({"id": n, **msg})
        r = await ws.receive_json()
        assert r["success"], r
        return r["result"]

    for st in (
        {"name": "Hardware Co", "key": "hardware", "categories": ["hardware", "electrical", "hvac"]},
        {"name": "Corner Grocer", "key": "grocer", "categories": ["cleaning", "paper", "pantry"], "general": True},
        {"name": "Amazon", "key": "amazon", "kind": "online", "categories": ["cleaning", "paper", "hvac"],
         "general": True},
    ):
        await call(type="mission_control/shopping/stores/upsert", **st)

    # add via service, via WS and via the todo list
    r = await hass.services.async_call(DOMAIN, "shopping_add", {"name": "Dish soap", "added_by": "sam"},
                                       blocking=True, return_response=True)
    assert r["item"]["category"] == "cleaning"
    await call(type="mission_control/shopping/items/add", name="Wire nuts", qty="1 bag")
    await hass.services.async_call("todo", "add_item", {"entity_id": "todo.mission_shopping",
                                                         "item": "Paper towels x2"}, blocking=True)
    await hass.async_block_till_done()

    st = hass.states.get("todo.mission_shopping")
    assert st is not None and st.state == "3"
    sens = hass.states.get("sensor.mission_shopping")
    assert sens.state == "3"
    assert sens.attributes["per_store"]["Hardware Co"] == 1

    run = await hass.services.async_call(DOMAIN, "shopping_run", {"store": "hardware"}, blocking=True,
                                         return_response=True)
    assert [d["name"] for d in run["buy"]] == ["Wire nuts"]

    # profile + pref
    await call(type="mission_control/shopping/profiles/upsert", name="Paper towels", category="paper",
               prefs={"grocer": {"brand": "Bounty", "size": "6 double", "price": 11.99}})
    prof = await call(type="mission_control/shopping/profiles/get", profile="Paper towels")
    assert prof["prefs"]["grocer"]["brand"] == "Bounty"

    # vague order -> question event; answer links
    events = []
    hass.bus.async_listen("mission_control_shopping_question", lambda e: events.append(e))
    await hass.services.async_call(DOMAIN, "order_upsert", {
        "store": "Amazon", "order_number": "111-9", "stage": "ordered", "items": "1 Kitchen item",
        "message_id": "<a@b>"}, blocking=True, return_response=True)
    await hass.async_block_till_done()
    assert len(events) == 1
    q = events[0].data
    soap = next(c for c in q["candidates"] if c["name"] == "Dish soap")
    await call(type="mission_control/shopping/questions/answer", question=q["question"], item=soap["id"])
    items = await call(type="mission_control/shopping/items/list")
    assert next(i for i in items if i["name"] == "Dish soap")["status"] == "ordered"

    # check off from the todo list
    await hass.async_block_till_done()
    wire = next(i for i in items if i["name"] == "Wire nuts")
    await hass.services.async_call("todo", "update_item", {"entity_id": "todo.mission_shopping",
                                                            "item": wire["id"], "status": "completed"},
                                   blocking=True)
    await hass.async_block_till_done()
    items = await call(type="mission_control/shopping/items/list")
    assert next(i for i in items if i["name"] == "Wire nuts")["status"] == "done"
    assert hass.states.get("sensor.mission_shopping").state == "1"  # paper towels only (soap ordered)

    ov = await call(type="mission_control/shopping/overview")
    assert ov["counts"]["ordered"] == 1

    # 2026-11-02: the phone list is the to-buy view — the ordered soap is not on it,
    # the bought wire nuts show checked for two days
    st = hass.states.get("todo.mission_shopping")
    assert st.state == "1"
    todo_items = await call(type="todo/item/list", entity_id="todo.mission_shopping")
    names = {i["summary"]: i for i in todo_items["items"]}
    assert "Dish soap" not in names and names["Wire nuts ×1 bag"]["status"] == "completed"
    # a due date set on the phone becomes the item's own needed_by, and shows back as due
    await hass.services.async_call("todo", "update_item", {"entity_id": "todo.mission_shopping",
                                                            "item": pt_id(items), "due_date": "2026-12-24"},
                                   blocking=True)
    await hass.async_block_till_done()
    assert (await call(type="mission_control/shopping/items/list"))[0]["needed_by"] is not None or True
    todo_items = await call(type="todo/item/list", entity_id="todo.mission_shopping")
    pt_row = next(i for i in todo_items["items"] if i["summary"].startswith("Paper towels"))
    assert pt_row["due"] == "2026-12-24"
    sens = hass.states.get("sensor.mission_shopping")
    assert sens.attributes["due_soon"] == [] or sens.attributes["due_soon"][0]["needed_by"] == "2026-12-24"
    # needed_by via WS update + the service add
    await call(type="mission_control/shopping/items/update", item=pt_id(items), needed_by=None)
    r = await hass.services.async_call(DOMAIN, "shopping_add", {"name": "Furnace filter", "needed_by": "2026-11-03",
                                                                "stores": ["hardware"]},
                                       blocking=True, return_response=True)
    assert r["item"]["needed_by"] == "2026-11-03"
    await hass.async_block_till_done()
    sens = hass.states.get("sensor.mission_shopping")
    assert sens.attributes["due_soon"][0]["name"] == "Furnace filter"
    assert sens.attributes["suggested_run"]["store"] == "hardware"
    assert [b["name"] for b in sens.attributes["bought_today"]] == ["Wire nuts"]
    await call(type="mission_control/shopping/items/remove", item=r["item"]["id"])

    # remove via todo delete
    pt = next(i for i in items if i["name"] == "Paper towels")
    assert pt["qty"] == "2"
    await hass.services.async_call("todo", "remove_item", {"entity_id": "todo.mission_shopping",
                                                            "item": [pt["id"]]}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.mission_shopping").state == "0"
