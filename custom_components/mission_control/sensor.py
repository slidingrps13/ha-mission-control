"""Mission Control sensors.

- sensor.mission_control_activity — what moved today (always).
- sensor.packages_* — order counts with compact lists (packages module).
- sensor.mission_shopping — the shopping list summary (shopping module).

Recomputed on every write and every 10 minutes (so 'today' rolls over at midnight).
"""

from __future__ import annotations

import datetime as dt
import logging

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from . import MissionConfigEntry, MissionData
from .const import CONF_PACKAGES, CONF_SHOPPING, SIGNAL_CHANGED, SIGNAL_ORDERS, SIGNAL_SHOPPING

_LOGGER = logging.getLogger(__name__)

# (key in listing(), entity name, icon)
SENSORS = (
    ("arriving_today", "Packages arriving today", "mdi:truck-delivery"),
    ("on_the_way", "Packages on the way", "mdi:package-variant-closed"),
    ("not_shipped", "Packages ordered not shipped", "mdi:cart-outline"),
    ("delivered_today", "Packages delivered today", "mdi:package-variant-closed-check"),
)
ATTR_KEYS = ("store", "order_number", "account", "stage", "delayed", "expected_date",
             "expected_window", "carrier", "tracking", "items", "total", "url",
             "card_uid", "carrier_seen", "delivered_at", "collected_at")


async def async_setup_entry(
    hass: HomeAssistant, entry: MissionConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    data = entry.runtime_data
    # Modules switched off in the options leave their sensors behind: drop them.
    ent_reg = er.async_get(hass)
    for reg in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
        if reg.domain != "sensor":
            continue
        uid = reg.unique_id or ""
        if (uid.startswith(f"{entry.entry_id}_orders_") and not entry.options.get(CONF_PACKAGES)) or (
            uid == f"{entry.entry_id}_shopping_list" and not entry.options.get(CONF_SHOPPING)
        ):
            ent_reg.async_remove(reg.entity_id)
    if entry.options.get(CONF_PACKAGES):
        ents = [OrdersSensor(data, entry.entry_id, *s) for s in SENSORS]
        async_add_entities(ents)

        async def _refresh(*_):
            try:
                listing = await data.run(data.store.order_listing)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("order listing failed")
                return
            for e in ents:
                e.apply(listing)

        @callback
        def _on_signal() -> None:
            hass.async_create_task(_refresh())

        entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_ORDERS, _on_signal))
        entry.async_on_unload(async_track_time_interval(hass, _refresh, dt.timedelta(minutes=10)))
        hass.async_create_task(_refresh())

    if entry.options.get(CONF_SHOPPING):
        # Shopping list (2026-10-29)
        shop = ShoppingSensor(data, entry.entry_id)
        async_add_entities([shop])

        async def _shop_refresh(*_):
            try:
                ov = await data.run(data.store.shop_overview)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("shopping overview failed")
                return
            shop.apply(ov)

        async def _shop_tick(*_):
            # REPEAT cards: bought filters come back as the next cycle nears.
            try:
                back = await data.run(data.store.shop_restock)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("shopping restock failed")
                back = []
            if back:
                from homeassistant.helpers.dispatcher import async_dispatcher_send  # noqa: PLC0415

                async_dispatcher_send(hass, SIGNAL_SHOPPING)
            else:
                await _shop_refresh()

        @callback
        def _on_shop() -> None:
            hass.async_create_task(_shop_refresh())

        entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_SHOPPING, _on_shop))
        entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_ORDERS, _on_shop))
        entry.async_on_unload(async_track_time_interval(hass, _shop_tick, dt.timedelta(minutes=10)))
        hass.async_create_task(_shop_tick())

    # Activity today (2026-11-02): what moved, for the 17:00 wrap push and the Digest page.
    act = ActivitySensor(data, entry.entry_id)
    async_add_entities([act])

    async def _act_refresh(*_):
        try:
            a = await data.run(data.store.activity_today)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("activity today failed")
            return
        act.apply(a)

    @callback
    def _on_any() -> None:
        hass.async_create_task(_act_refresh())

    entry.async_on_unload(async_dispatcher_connect(hass, f"{SIGNAL_CHANGED}_any", _on_any))
    entry.async_on_unload(async_track_time_interval(hass, _act_refresh, dt.timedelta(minutes=10)))
    hass.async_create_task(_act_refresh())


class OrdersSensor(SensorEntity):
    _attr_has_entity_name = False
    _attr_should_poll = False
    _attr_native_unit_of_measurement = "packages"

    def __init__(self, data: MissionData, entry_id: str, key: str, name: str, icon: str) -> None:
        self._data = data
        self._key = key
        self._attr_name = name
        self._attr_icon = icon
        self._attr_unique_id = f"{entry_id}_orders_{key}"
        self.entity_id = f"sensor.packages_{key}"
        self._attr_native_value = None
        self._attr_extra_state_attributes = {"orders": []}

    @callback
    def apply(self, listing: dict) -> None:
        rows = listing.get(self._key, [])
        self._attr_native_value = len(rows)
        attrs = {"orders": [{k: o.get(k) for k in ATTR_KEYS} for o in rows[:25]]}
        if self._key == "arriving_today":
            attrs["carriers"] = sorted({o.get("carrier") or "unknown" for o in rows})
            attrs["overdue"] = len(listing.get("overdue", []))
        self._attr_extra_state_attributes = attrs
        if self.hass:
            self.async_write_ha_state()


class ShoppingSensor(SensorEntity):
    """sensor.shopping_list — items still to buy; compact list + per-store counts."""

    _attr_has_entity_name = False
    _attr_should_poll = False
    _attr_native_unit_of_measurement = "items"
    _attr_icon = "mdi:cart-variant"

    def __init__(self, data: MissionData, entry_id: str) -> None:
        self._data = data
        self._attr_name = "Shopping list"
        self._attr_unique_id = f"{entry_id}_shopping_list"
        self.entity_id = "sensor.mission_shopping"
        self._attr_native_value = None
        self._attr_extra_state_attributes = {}

    @callback
    def apply(self, ov: dict) -> None:
        needed = [i for i in ov["items"] if i["status"] == "needed" and not i.get("handled")]
        self._attr_native_value = len(needed)
        names = {s["key"]: s["name"] for s in ov["stores"]}
        self._attr_extra_state_attributes = {
            "items": [
                {"id": i["id"], "name": i["name"], "qty": i.get("qty"), "status": i["status"],
                 "category": i.get("category"), "at": i.get("at"), "card": (i.get("card") or {}).get("title"),
                 "handled": i.get("handled"), "needed_by": i.get("needed_by_eff"),
                 "due_in": i.get("due_in")}
                for i in ov["items"][:40]
            ],
            "per_store": {names.get(k, k): len(v) for k, v in ov["groups"].items() if v},
            "ordered": ov["counts"].get("ordered", 0),
            "arriving": ov["counts"].get("arriving", 0),
            "running_low": [x["name"] for x in ov["running_low"][:10]],
            "questions": len(ov["questions"]),
            "est_total": ov["est_total"],
            # needed-by (2026-11-02): overdue first, then soonest — what the pushes read
            "due_soon": [
                {"name": i["name"], "needed_by": i["needed_by_eff"], "due_in": i["due_in"],
                 "at": [names.get(k, k) for k in (i.get("at") or [])][:3],
                 "card": (i.get("card") or {}).get("title")}
                for i in ov.get("due_soon", [])[:10]
            ],
            "overdue": sum(1 for i in ov.get("due_soon", []) if i["due_in"] < 0),
            "suggested_run": ov.get("suggested_run"),
            "bought_today": [
                {"name": b["name"], "store": names.get(b["store_key"], b["store_key"]), "source": b["source"]}
                for b in ov.get("bought_today", [])[:20]
            ],
        }
        if self.hass:
            self.async_write_ha_state()


class ActivitySensor(SensorEntity):
    """sensor.mission_control_activity — cards closed today (state) with what moved as attributes.

    Read from item_history since local midnight, so phone check-offs count even though they leave
    no note. Refreshed on every store change and every 10 minutes (midnight rollover)."""

    _attr_has_entity_name = False
    _attr_should_poll = False
    _attr_native_unit_of_measurement = "cards"
    _attr_icon = "mdi:history"

    def __init__(self, data: MissionData, entry_id: str) -> None:
        self._data = data
        self._attr_name = "Mission Control activity"
        self._attr_unique_id = f"{entry_id}_activity"
        self.entity_id = "sensor.mission_control_activity"
        self._attr_native_value = None
        self._attr_extra_state_attributes = {}

    @callback
    def apply(self, a: dict) -> None:
        self._attr_native_value = len(a["closed"])
        self._attr_extra_state_attributes = {
            "day": a["day"],
            "closed": a["closed"][:40],
            "moved": a["moved"][:40],
            "freed": a["freed"][:40],
            "created": a["created"][:40],
        }
        if self.hass:
            self.async_write_ha_state()
