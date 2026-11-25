"""A friend's first install: setup, the panel, the default branches, quick add,
the digests and the options flow — against a real Home Assistant core."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.mission_control.const import DOMAIN

DEFAULT_KEYS = ["home", "projects", "personal", "work", "automotive"]


@pytest.fixture(autouse=True)
def fresh_db(hass: HomeAssistant):
    """The test config dir is shared between tests: start every test with no database."""
    for name in ("mission_control.db", "mission_control.db-wal", "mission_control.db-shm"):
        Path(hass.config.path(name)).unlink(missing_ok=True)
    yield


async def _setup(hass: HomeAssistant, **user) -> config_entries.ConfigEntry:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    return hass.config_entries.async_entries(DOMAIN)[0]


async def _ws(hass_ws_client, hass, msg: dict):
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(msg)
    resp = await client.receive_json()
    assert resp["success"], resp
    return resp["result"]


async def test_fresh_install_serves_default_branches_and_panel(hass: HomeAssistant, hass_ws_client, hass_client) -> None:
    entry = await _setup(hass)
    assert entry.state is config_entries.ConfigEntryState.LOADED

    for k in DEFAULT_KEYS:
        assert hass.states.get(f"todo.{k}_tracker") is not None, k
    for v in ("todo.mission_ready_for_you", "todo.mission_this_week", "todo.mission_today"):
        assert hass.states.get(v) is not None, v
    assert hass.states.get("sensor.mission_control_activity") is not None
    # optional modules are off by default
    assert hass.states.get("todo.mission_shopping") is None
    assert hass.states.get("sensor.packages_arriving_today") is None
    assert not hass.services.has_service(DOMAIN, "order_upsert")
    assert not hass.services.has_service(DOMAIN, "shopping_add")
    assert hass.services.has_service(DOMAIN, "file_note")

    # the sidebar panel and its files
    assert "mission-control" in hass.data["frontend_panels"]
    client = await hass_client()
    for name in ("mission-control.js", "app.js"):
        r = await client.get(f"/mission_control_frontend/{name}")
        assert r.status == 200, name
        body = await r.text()
        assert "customElements.define" in body

    cfg = await _ws(hass_ws_client, hass, {"type": "mission_control/config"})
    assert [b["key"] for b in cfg["branches"]] == DEFAULT_KEYS
    assert cfg["packages"] is False and cfg["shopping"] is False


async def test_todo_add_and_ws_list(hass: HomeAssistant, hass_ws_client) -> None:
    await _setup(hass, person_name="Sam")
    await hass.services.async_call(
        "todo", "add_item",
        {"entity_id": "todo.home_tracker", "item": "Fix the porch light",
         "description": "LANE: need\nNEXT STEP: Sam: buy a bulb."},
        blocking=True,
    )
    res = await _ws(hass_ws_client, hass, {"type": "mission_control/items/list", "branch": "home"})
    assert res["count"] == 1
    item = res["items"][0]
    assert item["title"] == "Fix the porch light"
    assert item["owner_effective"] == "me"  # "Sam" is accepted as the person's name
    await hass.async_block_till_done()
    view = hass.states.get("todo.mission_ready_for_you")
    assert view.state == "1"


async def test_quick_add_without_ai_files_a_plain_card(hass: HomeAssistant) -> None:
    await _setup(hass)
    res = await hass.services.async_call(
        DOMAIN, "file_note", {"note": "Rotate the tires", "source": "panel", "pins": {"branch": "automotive"}},
        blocking=True, return_response=True,
    )
    assert res["status"] == "filed" and res["ai"] is False
    assert res["entity_id"] == "todo.automotive_tracker"
    await hass.async_block_till_done()
    assert hass.states.get("todo.automotive_tracker").state == "1"

    # Same title again without a model: an AI chat (source claude) is shown the match ...
    again = await hass.services.async_call(
        DOMAIN, "file_note", {"note": "rotate the tires!", "source": "claude"},
        blocking=True, return_response=True,
    )
    assert again["status"] == "duplicate" and again["duplicate_uid"] == res["uid"]
    # ... it can still insist ...
    forced = await hass.services.async_call(
        DOMAIN, "file_note", {"note": "Rotate the tires", "source": "claude", "force": True,
                              "pins": {"branch": "automotive"}},
        blocking=True, return_response=True,
    )
    assert forced["status"] == "filed"
    # ... and voice, with no screen to ask on, keeps the words on the existing card.
    voice = await hass.services.async_call(
        DOMAIN, "file_note", {"note": "Rotate the tires", "source": "voice"},
        blocking=True, return_response=True,
    )
    assert voice["status"] == "noted_on_duplicate"
    await hass.async_block_till_done()
    assert hass.states.get("todo.automotive_tracker").state == "2"


async def test_quick_add_with_ai_task_and_duplicate(hass: HomeAssistant) -> None:
    await _setup(hass, ai_task_entity="ai_task.fake")
    hass.states.async_set("ai_task.fake", "unknown")
    calls: list[dict] = []
    reply: dict = {}

    async def fake_generate(call):
        calls.append(dict(call.data))
        return {"conversation_id": "x", "data": "Sure:\n" + json.dumps(reply)}

    hass.services.async_register("ai_task", "generate_data", fake_generate,
                                 supports_response=SupportsResponse.ONLY)
    today = dt_util.now().date().isoformat()
    reply.update({"cards": [{"title": "Change the oil", "branch": "automotive", "lane": "need", "toolbox": "Car",
                             "effort": "1h", "cost": "$45", "owner": "me", "next_step": "Buy 5 quarts and a filter.",
                             "due": today, "tags": [], "repeat": ""}],
                  "duplicate_of": "", "covers": []})
    res = await hass.services.async_call(DOMAIN, "file_note", {"note": "oil change today", "source": "panel"},
                                         blocking=True, return_response=True)
    assert res["status"] == "filed" and res["ai"] is True
    assert "- automotive (Automotive" in calls[0]["instructions"]
    uid = res["uid"]

    reply.update({"duplicate_of": uid[:8]})
    res2 = await hass.services.async_call(DOMAIN, "file_note", {"note": "do the oil", "source": "voice"},
                                          blocking=True, return_response=True)
    assert res2["status"] == "noted_on_duplicate"
    await hass.async_block_till_done()
    assert hass.states.get("todo.automotive_tracker").state == "1"

    # due today + mine → the morning plan lists it
    plan = await hass.services.async_call(DOMAIN, "digest", {"kind": "morning"}, blocking=True, return_response=True)
    assert "Do today: Change the oil (1h)" in plan["message"]
    # closing it (as a phone check-off would) shows in the evening wrap
    await hass.async_block_till_done()
    await hass.services.async_call("todo", "update_item",
                                   {"entity_id": "todo.automotive_tracker", "item": uid, "status": "completed"},
                                   blocking=True)
    await hass.async_block_till_done()
    act = hass.states.get("sensor.mission_control_activity")
    assert act.state == "1" and act.attributes["closed"][0]["title"] == "Change the oil"
    wrap = await hass.services.async_call(DOMAIN, "digest", {"kind": "evening"}, blocking=True, return_response=True)
    assert wrap["title"].endswith("what moved")
    assert wrap["message"].startswith("Done: Change the oil")


async def test_options_add_edit_remove_branch_and_modules(hass: HomeAssistant) -> None:
    entry = await _setup(hass)

    async def run(step: str, data: dict | None = None, follow: dict | None = None):
        r = await hass.config_entries.options.async_init(entry.entry_id)
        assert r["type"] is FlowResultType.MENU
        r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": step})
        r = await hass.config_entries.options.async_configure(r["flow_id"], data or {})
        if follow is not None:
            r = await hass.config_entries.options.async_configure(r["flow_id"], follow)
        await hass.async_block_till_done()
        return r

    r = await run("add_branch", {"name": "Garden", "icon": "mdi:sprout", "keywords": "seeds, compost"})
    assert r["type"] is FlowResultType.CREATE_ENTRY
    assert hass.states.get("todo.garden_tracker") is not None

    r = await run("edit_branch", {"branch": "garden"}, {"name": "Yard & garden", "icon": "mdi:sprout"})
    assert r["type"] is FlowResultType.CREATE_ENTRY
    assert [b["name"] for b in entry.options["branches"] if b["key"] == "garden"] == ["Yard & garden"]

    await hass.services.async_call("todo", "add_item", {"entity_id": "todo.work_tracker", "item": "Send report"},
                                   blocking=True)
    r = await run("remove_branch", {"branch": "work"})
    assert r["type"] is FlowResultType.FORM and r["errors"] == {"branch": "branch_not_empty"}

    r = await run("remove_branch", {"branch": "garden"})
    assert r["type"] is FlowResultType.CREATE_ENTRY
    assert hass.states.get("todo.garden_tracker") is None
    assert er.async_get(hass).async_get("todo.garden_tracker") is None

    r = await run("settings", {"packages": True, "shopping": True})
    assert r["type"] is FlowResultType.CREATE_ENTRY
    assert hass.states.get("todo.mission_shopping") is not None
    assert hass.services.has_service(DOMAIN, "order_upsert")
    # an order feed: any automation can call order_upsert; a card waits on it
    await hass.services.async_call(DOMAIN, "order_upsert",
                                   {"store": "Example Shop", "order_number": "A-1001", "stage": "shipped",
                                    "items": "Bike pump"}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.packages_on_the_way").state == "1"

    r = await run("settings", {"packages": False, "shopping": False})
    assert hass.states.get("todo.mission_shopping") is None
    assert not hass.services.has_service(DOMAIN, "shopping_add")


async def test_unload_and_remove_entry_drop_the_panel(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert "mission-control" not in hass.data.get("frontend_panels", {})
