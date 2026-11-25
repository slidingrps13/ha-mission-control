"""WebSocket API for the house shopping list (2026-10-29): mission_control/shopping/*.

Reachable through the ha-bridge (ha_ws), the Shopping page (/local/shop_app.html)
and any Claude session.  Every write refreshes sensor.shopping_list and
todo.shopping_list (SIGNAL_SHOPPING) and, when a card is touched, that card's list.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import DOMAIN, SIGNAL_ORDERS

_REGISTERED = f"{DOMAIN}_shopping_ws_registered"


def _data(hass: HomeAssistant):
    from . import get_data  # noqa: PLC0415

    return get_data(hass)


async def _changed(hass: HomeAssistant, data, card_uid: str | None = None) -> None:
    from . import shopping_changed  # noqa: PLC0415

    await shopping_changed(hass, data, card_uid)


async def _call(hass, connection, msg, fn, *args, card_of=None, orders=False) -> None:
    data = _data(hass)
    try:
        res = await data.run(fn, *args)
    except (HomeAssistantError, ValueError) as err:
        connection.send_error(msg["id"], "failed", str(err))
        return
    card = card_of(res) if card_of else None
    if card_of is not None or msg["type"].split("/")[-1] not in ("list", "get", "overview", "run"):
        await _changed(hass, data, card)
    if orders:
        async_dispatcher_send(hass, SIGNAL_ORDERS)
    connection.send_result(msg["id"], res)


def _item_card(res: Any) -> str | None:
    it = res.get("item", res) if isinstance(res, dict) else None
    return (it or {}).get("card_uid") if isinstance(it, dict) else None


P = "mission_control/shopping/"
_STORES = vol.Any(None, str, [str])


@websocket_api.websocket_command({vol.Required("type"): P + "overview"})
@websocket_api.async_response
async def ws_overview(hass, connection, msg):
    await _call(hass, connection, msg, _data(hass).store.shop_overview)


@websocket_api.websocket_command(
    {vol.Required("type"): P + "items/list",
     vol.Optional("status", default="open"): vol.In(
         ["open", "all", "needed", "ordered", "arriving", "done", "dismissed"])}
)
@websocket_api.async_response
async def ws_items_list(hass, connection, msg):
    await _call(hass, connection, msg, _data(hass).store.shop_items, msg["status"])


@websocket_api.websocket_command(
    {vol.Required("type"): P + "items/add", vol.Required("name"): str,
     vol.Optional("qty"): vol.Any(None, str), vol.Optional("stores"): _STORES,
     vol.Optional("category"): vol.Any(None, str), vol.Optional("card_uid"): vol.Any(None, str),
     vol.Optional("note"): vol.Any(None, str), vol.Optional("handled"): vol.Any(None, str),
     vol.Optional("needed_by"): vol.Any(None, str),
     vol.Optional("added_by"): vol.Any(None, str), vol.Optional("source"): vol.Any(None, str)}
)
@websocket_api.async_response
async def ws_items_add(hass, connection, msg):
    payload = {k: v for k, v in msg.items() if k not in ("type", "id")}
    await _call(hass, connection, msg, _data(hass).store.shop_add, payload, card_of=_item_card)


@websocket_api.websocket_command(
    {vol.Required("type"): P + "items/update", vol.Required("item"): str,
     vol.Optional("name"): str, vol.Optional("qty"): vol.Any(None, str),
     vol.Optional("stores"): _STORES, vol.Optional("category"): vol.Any(None, str),
     vol.Optional("card_uid"): vol.Any(None, str), vol.Optional("note"): vol.Any(None, str),
     vol.Optional("handled"): vol.Any(None, str), vol.Optional("profile"): vol.Any(None, str),
     vol.Optional("needed_by"): vol.Any(None, str),
     vol.Optional("status"): vol.In(["needed", "ordered", "arriving", "done", "dismissed"])}
)
@websocket_api.async_response
async def ws_items_update(hass, connection, msg):
    fields = {k: v for k, v in msg.items() if k not in ("type", "id", "item")}
    await _call(hass, connection, msg, _data(hass).store.shop_update, msg["item"], fields,
                card_of=_item_card)


@websocket_api.websocket_command(
    {vol.Required("type"): P + "items/check", vol.Required("item"): str,
     vol.Optional("store"): vol.Any(None, str),
     vol.Optional("price"): vol.Any(None, str, float, int)}
)
@websocket_api.async_response
async def ws_items_check(hass, connection, msg):
    await _call(hass, connection, msg, _data(hass).store.shop_check, msg["item"], msg.get("store"),
                msg.get("price"), "check", card_of=_item_card)


@websocket_api.websocket_command({vol.Required("type"): P + "items/uncheck", vol.Required("item"): str})
@websocket_api.async_response
async def ws_items_uncheck(hass, connection, msg):
    await _call(hass, connection, msg, _data(hass).store.shop_uncheck, msg["item"], card_of=_item_card)


@websocket_api.websocket_command({vol.Required("type"): P + "items/remove", vol.Required("item"): str})
@websocket_api.async_response
async def ws_items_remove(hass, connection, msg):
    await _call(hass, connection, msg, _data(hass).store.shop_update, msg["item"],
                {"status": "dismissed"}, card_of=_item_card)


@websocket_api.websocket_command({vol.Required("type"): P + "run", vol.Required("store"): str})
@websocket_api.async_response
async def ws_run(hass, connection, msg):
    await _call(hass, connection, msg, _data(hass).store.shop_run, msg["store"])


@websocket_api.websocket_command({vol.Required("type"): P + "stores/list"})
@websocket_api.async_response
async def ws_stores_list(hass, connection, msg):
    await _call(hass, connection, msg, _data(hass).store.shop_stores)


@websocket_api.websocket_command(
    {vol.Required("type"): P + "stores/upsert", vol.Required("name"): str,
     vol.Optional("key"): str, vol.Optional("kind"): vol.In(["in_store", "online"]),
     vol.Optional("categories"): [str], vol.Optional("add_categories"): [str],
     vol.Optional("general"): bool, vol.Optional("zone"): vol.Any(None, str),
     vol.Optional("address"): vol.Any(None, str), vol.Optional("aliases"): [str]}
)
@websocket_api.async_response
async def ws_stores_upsert(hass, connection, msg):
    payload = {k: v for k, v in msg.items() if k not in ("type", "id")}
    await _call(hass, connection, msg, _data(hass).store.shop_store_upsert, payload)


@websocket_api.websocket_command({vol.Required("type"): P + "stores/delete", vol.Required("store"): str})
@websocket_api.async_response
async def ws_stores_delete(hass, connection, msg):
    await _call(hass, connection, msg, _data(hass).store.shop_store_delete, msg["store"])


@websocket_api.websocket_command(
    {vol.Required("type"): P + "profiles/list", vol.Optional("q"): vol.Any(None, str)}
)
@websocket_api.async_response
async def ws_profiles_list(hass, connection, msg):
    await _call(hass, connection, msg, _data(hass).store.shop_profiles, msg.get("q"))


@websocket_api.websocket_command({vol.Required("type"): P + "profiles/get", vol.Required("profile"): str})
@websocket_api.async_response
async def ws_profiles_get(hass, connection, msg):
    data = _data(hass)
    p = await data.run(data.store.shop_profile_get, msg["profile"])
    if p is None:
        connection.send_error(msg["id"], "not_found", "no profile matches")
        return
    connection.send_result(msg["id"], p)


@websocket_api.websocket_command(
    {vol.Required("type"): P + "profiles/upsert", vol.Optional("profile_id"): str,
     vol.Optional("name"): str,
     vol.Optional("category"): vol.Any(None, str), vol.Optional("aliases"): [str],
     vol.Optional("add_aliases"): [str], vol.Optional("bulk_store"): vol.Any(None, str),
     vol.Optional("topup_store"): vol.Any(None, str), vol.Optional("star"): bool,
     vol.Optional("handled"): vol.Any(None, str), vol.Optional("notes"): vol.Any(None, str),
     vol.Optional("prefs"): {str: dict}}
)
@websocket_api.async_response
async def ws_profiles_upsert(hass, connection, msg):
    # 'id' is the WS message id: a profile is addressed by name or profile_id
    payload = {k: v for k, v in msg.items() if k not in ("type", "id", "profile_id")}
    if msg.get("profile_id"):
        payload["id"] = msg["profile_id"]
    await _call(hass, connection, msg, _data(hass).store.shop_profile_upsert, payload)


@websocket_api.websocket_command({vol.Required("type"): P + "profiles/delete", vol.Required("profile"): str})
@websocket_api.async_response
async def ws_profiles_delete(hass, connection, msg):
    await _call(hass, connection, msg, _data(hass).store.shop_profile_delete, msg["profile"])


@websocket_api.websocket_command(
    {vol.Required("type"): P + "purchases/add", vol.Required("name"): str,
     vol.Optional("profile"): vol.Any(None, str), vol.Optional("store"): vol.Any(None, str),
     vol.Optional("ts"): vol.Any(None, str), vol.Optional("qty"): vol.Any(None, str),
     vol.Optional("price"): vol.Any(None, str, float, int), vol.Optional("order_key"): vol.Any(None, str),
     vol.Optional("source"): vol.Any(None, str)}
)
@websocket_api.async_response
async def ws_purchases_add(hass, connection, msg):
    payload = {k: v for k, v in msg.items() if k not in ("type", "id")}
    await _call(hass, connection, msg, _data(hass).store.shop_purchase_add, payload)


@websocket_api.websocket_command({vol.Required("type"): P + "questions/list"})
@websocket_api.async_response
async def ws_questions_list(hass, connection, msg):
    await _call(hass, connection, msg, _data(hass).store.shop_questions)


@websocket_api.websocket_command(
    {vol.Required("type"): P + "questions/answer", vol.Required("question"): str,
     vol.Optional("item"): vol.Any(None, str)}
)
@websocket_api.async_response
async def ws_questions_answer(hass, connection, msg):
    await _call(hass, connection, msg, _data(hass).store.shop_answer, msg["question"], msg.get("item"),
                orders=True)


@websocket_api.websocket_command(
    {vol.Required("type"): P + "link_order", vol.Required("item"): str, vol.Required("order"): str}
)
@websocket_api.async_response
async def ws_link_order(hass, connection, msg):
    await _call(hass, connection, msg, _data(hass).store.shop_link_order, msg["item"], msg["order"],
                card_of=_item_card, orders=True)


@websocket_api.websocket_command({vol.Required("type"): P + "not_low", vol.Required("profile"): str})
@websocket_api.async_response
async def ws_not_low(hass, connection, msg):
    await _call(hass, connection, msg, _data(hass).store.shop_not_low, msg["profile"])


@websocket_api.websocket_command({vol.Required("type"): P + "sync_orders"})
@websocket_api.async_response
async def ws_sync_orders(hass, connection, msg):
    await _call(hass, connection, msg, _data(hass).store.shop_sync_orders, orders=True)


@websocket_api.websocket_command({vol.Required("type"): P + "restock"})
@websocket_api.async_response
async def ws_restock(hass, connection, msg):
    await _call(hass, connection, msg, _data(hass).store.shop_restock)


@callback
def async_register(hass: HomeAssistant) -> None:
    if hass.data.get(_REGISTERED):
        return
    hass.data[_REGISTERED] = True
    for cmd in (ws_overview, ws_items_list, ws_items_add, ws_items_update, ws_items_check,
                ws_items_uncheck, ws_items_remove, ws_run, ws_stores_list, ws_stores_upsert,
                ws_stores_delete, ws_profiles_list, ws_profiles_get, ws_profiles_upsert,
                ws_profiles_delete, ws_purchases_add, ws_questions_list, ws_questions_answer,
                ws_link_order, ws_not_low, ws_restock, ws_sync_orders):
        websocket_api.async_register_command(hass, cmd)
