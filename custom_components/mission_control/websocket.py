"""WebSocket API: mission_control/items/list, items/get, items/update,
notes/add, links/add.  Reachable through the ha-bridge (ha_ws) and the panel."""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from .codec import COLUMNS, parse_tags
from .const import (
    BRANCH_CONF,
    BRANCHES,
    CONF_AI_TASK,
    CONF_CALENDAR,
    CONF_PACKAGES,
    CONF_PERSON,
    CONF_SHOPPING,
    PANEL_URL,
    VERSION,
    CLAUDE_BRANCHES,
    DOMAIN,
    HANDS_ON,
    OWNER_AI,
    OWNER_ME,
    WORK_BRANCHES,
    owner_key,
    valid_branch,
)

_REGISTERED = f"{DOMAIN}_ws_registered"

ROW_FIELDS = (
    "uid",
    "branch",
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


def owner_of(row: dict) -> str:
    """Whose move a card is: "me" or "claude" (the AI assistant). Mirrors app.js ownerOf.

    Explicit OWNER wins.  Otherwise a next step addressed to the person ("You …",
    "Decide …"), a hands-on toolbox, a trade visit or something to buy first is
    theirs; on an assistant branch the rest is the assistant's.
    """
    own = owner_key(row.get("owner"))
    if own:
        return own
    ns = row.get("next_step") or ""
    first = (re.match(r"^\s*(\w+)", ns) or [None, ""])[1].lower()
    if first in ("you", "decide") or owner_key(first) == OWNER_ME:
        return OWNER_ME
    tb = row.get("toolbox") or ""
    if tb in HANDS_ON or tb.lower().endswith(" visit") or row.get("buy_first"):
        return OWNER_ME
    return OWNER_AI if row["branch"] in CLAUDE_BRANCHES else OWNER_ME


def lane_of(row: dict) -> str:
    lane = (row.get("lane") or "").lower()
    return lane or ("work" if row["branch"] in WORK_BRANCHES else "")


def _data(hass: HomeAssistant):
    from . import get_data  # noqa: PLC0415  (avoid an import cycle)

    return get_data(hass)


def _shape(store, row: dict, fields: list[str] | None, with_desc: bool) -> dict:
    out: dict[str, Any] = {k: row.get(k) for k in ROW_FIELDS}
    out["lane_effective"] = lane_of(row)
    out["owner_effective"] = owner_of(row)
    out["notes"] = [{"ts": ts, "text": tx} for ts, tx in row["notes"]]
    out["last_note"] = out["notes"][-1] if out["notes"] else None
    if with_desc:
        out["description"] = store.describe(row)
    if fields:
        out = {k: v for k, v in out.items() if k in fields}
    return out


@callback
def async_register(hass: HomeAssistant) -> None:
    if hass.data.get(_REGISTERED):
        return
    hass.data[_REGISTERED] = True
    for cmd in (ws_config, ws_list, ws_get, ws_update, ws_note, ws_link, ws_block_clear, ws_history, ws_revert, ws_changed, ws_orders_list, ws_order_get, ws_order_update, ws_order_merge):
        websocket_api.async_register_command(hass, cmd)


@websocket_api.websocket_command({vol.Required("type"): "mission_control/config"})
@callback
def ws_config(hass: HomeAssistant, connection, msg: dict) -> None:
    """What the panel needs to draw this install: branches, names, optional modules."""
    from . import get_entry  # noqa: PLC0415

    o = get_entry(hass).options
    connection.send_result(
        msg["id"],
        {
            "version": VERSION,
            "branches": [dict(BRANCH_CONF[k]) for k in BRANCHES],
            "person_name": o.get(CONF_PERSON) or "",
            "ai_task": o.get(CONF_AI_TASK) or None,
            "calendar": o.get(CONF_CALENDAR) or None,
            "packages": bool(o.get(CONF_PACKAGES)),
            "shopping": bool(o.get(CONF_SHOPPING)),
            "panel_url": PANEL_URL,
        },
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): "mission_control/items/list",
        vol.Optional("branch"): vol.All(
            lambda v: [v] if isinstance(v, str) else v, [valid_branch]
        ),
        vol.Optional("status", default="needs_action"): vol.In(
            ["needs_action", "completed", "all"]
        ),
        vol.Optional("lane"): str,
        vol.Optional("toolbox"): str,
        vol.Optional("owner"): vol.In([OWNER_ME, OWNER_AI]),
        vol.Optional("stale_days"): vol.All(int, vol.Range(min=0)),
        vol.Optional("q"): str,
        vol.Optional("blocked"): bool,
        vol.Optional("include_tagged", default=False): bool,
        vol.Optional("fields"): [str],
        vol.Optional("description", default=False): bool,
        vol.Optional("limit", default=500): vol.All(int, vol.Range(min=1, max=2000)),
    }
)
@websocket_api.async_response
async def ws_list(hass: HomeAssistant, connection, msg: dict) -> None:
    data = _data(hass)
    store = data.store
    rows = await data.run(store.items)
    branches = msg.get("branch")
    cutoff = None
    if "stale_days" in msg:
        cutoff = dt_util.utcnow() - dt.timedelta(days=msg["stale_days"])
    q = (msg.get("q") or "").lower()
    out = []
    for r in rows:
        tags = parse_tags(r.get("tags"), r["branch"])
        if branches and r["branch"] not in branches and not (
            msg["include_tagged"] and any(t in branches for t in tags)
        ):
            continue
        if msg["status"] != "all" and r["status"] != msg["status"]:
            continue
        if "lane" in msg and lane_of(r) != msg["lane"].lower():
            continue
        if "toolbox" in msg and (r.get("toolbox") or "").lower() != msg["toolbox"].lower():
            continue
        if "owner" in msg and owner_of(r) != msg["owner"]:
            continue
        if "blocked" in msg and bool(r.get("blocked_by")) != msg["blocked"]:
            continue
        if cutoff is not None:
            upd = dt.datetime.fromisoformat(r["updated_at"])
            if upd > cutoff:
                continue
        if q and not any(
            q in (r.get(k) or "").lower()
            for k in ("title", "next_step", "details", "toolbox", "uid")
        ):
            continue
        row = _shape(store, r, msg.get("fields"), msg["description"])
        if not msg.get("fields") or "tag_branches" in msg["fields"]:
            row["tag_branches"] = tags
        out.append(row)
        if len(out) >= msg["limit"]:
            break
    connection.send_result(msg["id"], {"count": len(out), "items": out})


@websocket_api.websocket_command(
    {vol.Required("type"): "mission_control/items/get", vol.Required("uid"): str}
)
@websocket_api.async_response
async def ws_get(hass: HomeAssistant, connection, msg: dict) -> None:
    data = _data(hass)
    store = data.store

    def _get():
        uid = store.resolve(msg["uid"])
        if uid is None:
            return None
        row = store.get(uid)
        return row, store.links(uid), store.branches_of(uid), store.blocks_of(uid), store.history(uid, 20)

    got = await data.run(_get)
    if got is None:
        connection.send_error(msg["id"], "not_found", "no unique item for that uid")
        return
    row, links, tags, blocks, history = got
    out = _shape(store, row, None, True)
    out["links"] = links
    out["tags"] = tags
    out["blocks"] = blocks
    out["history"] = history
    out["note_added"] = row["note_added"]
    connection.send_result(msg["id"], out)


EDIT_KEYS = {
    vol.Optional(k): vol.Any(None, str)
    for k in ("title", "due", "details", *COLUMNS)
}


@websocket_api.websocket_command(
    {
        vol.Required("type"): "mission_control/items/update",
        vol.Required("uid"): str,
        vol.Optional("status"): vol.In(["needs_action", "completed"]),
        vol.Optional("branch"): valid_branch,
        vol.Optional("source", default="ws"): str,
        **EDIT_KEYS,
    }
)
@websocket_api.async_response
async def ws_update(hass: HomeAssistant, connection, msg: dict) -> None:
    data = _data(hass)
    store = data.store
    changes = {k: v for k, v in msg.items() if k not in ("type", "id", "uid", "source")}
    if not changes:
        connection.send_error(msg["id"], "invalid", "nothing to change")
        return

    def _do():
        uid = store.resolve(msg["uid"])
        if uid is None:
            raise HomeAssistantError("no unique item for that uid")
        return uid, store.update_fields(uid, changes, msg["source"])

    try:
        uid, (old_b, new_b) = await data.run(_do)
    except HomeAssistantError as err:
        connection.send_error(msg["id"], "failed", str(err))
        return
    data.changed(old_b, new_b)
    row = await data.run(store.get, uid)
    connection.send_result(msg["id"], _shape(store, row, None, True))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "mission_control/notes/add",
        vol.Required("uid"): str,
        vol.Required("text"): str,
        vol.Optional("ts"): str,
        vol.Optional("source", default="ws"): str,
    }
)
@websocket_api.async_response
async def ws_note(hass: HomeAssistant, connection, msg: dict) -> None:
    data = _data(hass)
    store = data.store

    def _do():
        uid = store.resolve(msg["uid"])
        if uid is None:
            raise HomeAssistantError("no unique item for that uid")
        return uid, store.add_note(uid, msg["text"], msg.get("ts"), msg["source"])

    try:
        uid, branch = await data.run(_do)
    except (HomeAssistantError, ValueError) as err:
        connection.send_error(msg["id"], "failed", str(err))
        return
    data.changed(branch)
    row = await data.run(store.get, uid)
    connection.send_result(msg["id"], _shape(store, row, None, True))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "mission_control/links/add",
        vol.Required("from"): str,
        vol.Required("to"): str,
        vol.Required("kind"): vol.In(["parent", "blocked_by", "duplicate"]),
    }
)
@websocket_api.async_response
async def ws_link(hass: HomeAssistant, connection, msg: dict) -> None:
    data = _data(hass)
    try:
        branch = await data.run(
            data.store.add_link, msg["from"], msg["to"], msg["kind"]
        )
    except HomeAssistantError as err:
        connection.send_error(msg["id"], "failed", str(err))
        return
    data.changed(branch)
    uid = await data.run(data.store.resolve, msg["from"])
    connection.send_result(msg["id"], {"links": await data.run(data.store.links, uid)})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "mission_control/blocks/clear",
        vol.Required("uid"): str,
        vol.Required("seq"): int,
        vol.Optional("by", default=OWNER_ME): str,
    }
)
@websocket_api.async_response
async def ws_block_clear(hass: HomeAssistant, connection, msg: dict) -> None:
    """A person clears one block — the only way a text block clears."""
    data = _data(hass)
    store = data.store

    def _do():
        uid = store.resolve(msg["uid"])
        if uid is None:
            raise HomeAssistantError("no unique item for that uid")
        return uid, store.clear_block(uid, msg["seq"], msg["by"])

    try:
        uid, freed = await data.run(_do)
    except HomeAssistantError as err:
        connection.send_error(msg["id"], "failed", str(err))
        return
    row = await data.run(store.get, uid)
    data.changed(row["branch"])
    out = _shape(store, row, None, True)
    out["blocks"] = await data.run(store.blocks_of, uid)
    out["freed"] = bool(freed)
    connection.send_result(msg["id"], out)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "mission_control/items/history",
        vol.Required("uid"): str,
        vol.Optional("limit", default=50): int,
    }
)
@websocket_api.async_response
async def ws_history(hass: HomeAssistant, connection, msg: dict) -> None:
    """Field history of one card, newest first."""
    data = _data(hass)
    store = data.store
    uid = await data.run(store.resolve, msg["uid"])
    if uid is None:
        connection.send_error(msg["id"], "not_found", "no unique item for that uid")
        return
    connection.send_result(
        msg["id"], {"uid": uid, "history": await data.run(store.history, uid, msg["limit"])}
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): "mission_control/items/revert",
        vol.Required("history_id"): int,
        vol.Optional("source", default="revert"): str,
    }
)
@websocket_api.async_response
async def ws_revert(hass: HomeAssistant, connection, msg: dict) -> None:
    """Write a history row's old value back as a new change (undo)."""
    data = _data(hass)
    store = data.store
    try:
        uid, old_b, new_b = await data.run(store.revert, msg["history_id"], msg["source"])
    except HomeAssistantError as err:
        connection.send_error(msg["id"], "failed", str(err))
        return
    data.changed(old_b, new_b)
    row = await data.run(store.get, uid)
    out = _shape(store, row, None, True)
    out["history"] = await data.run(store.history, uid, 20)
    connection.send_result(msg["id"], out)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "mission_control/items/changed",
        vol.Required("since"): str,
        vol.Optional("branch"): valid_branch,
    }
)
@websocket_api.async_response
async def ws_changed(hass: HomeAssistant, connection, msg: dict) -> None:
    """Cards with recorded changes after an ISO timestamp — branch reviews read this."""
    data = _data(hass)
    rows = await data.run(data.store.changed_since, msg["since"], msg.get("branch"))
    connection.send_result(msg["id"], {"since": msg["since"], "items": rows})


# ------------------------------------------------------------- package tracker
@websocket_api.websocket_command(
    {vol.Required("type"): "mission_control/orders/list", vol.Optional("recent_days", default=3): int}
)
@websocket_api.async_response
async def ws_orders_list(hass: HomeAssistant, connection, msg: dict) -> None:
    data = _data(hass)
    connection.send_result(msg["id"], await data.run(data.store.order_listing, msg["recent_days"]))


@websocket_api.websocket_command(
    {vol.Required("type"): "mission_control/orders/get", vol.Required("order"): str}
)
@websocket_api.async_response
async def ws_order_get(hass: HomeAssistant, connection, msg: dict) -> None:
    data = _data(hass)
    o = await data.run(data.store.order_find, msg["order"])
    if o is None:
        connection.send_error(msg["id"], "not_found", "no order matches")
        return
    connection.send_result(msg["id"], o)


@websocket_api.websocket_command(
    {vol.Required("type"): "mission_control/orders/merge", vol.Required("into"): str,
     vol.Required("from"): str}
)
@websocket_api.async_response
async def ws_order_merge(hass: HomeAssistant, connection, msg: dict) -> None:
    """Fold a duplicate order (same shipment under another store name) into the real one."""
    from homeassistant.helpers.dispatcher import async_dispatcher_send  # noqa: PLC0415
    from .const import SIGNAL_ORDERS, SIGNAL_SHOPPING  # noqa: PLC0415

    data = _data(hass)
    try:
        o = await data.run(data.store.order_merge, msg["into"], msg["from"])
    except (HomeAssistantError, ValueError) as err:
        connection.send_error(msg["id"], "failed", str(err))
        return
    async_dispatcher_send(hass, SIGNAL_ORDERS)
    async_dispatcher_send(hass, SIGNAL_SHOPPING)
    connection.send_result(msg["id"], o)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "mission_control/orders/update",
        vol.Required("order"): str,
        vol.Optional("card_uid"): vol.Any(None, str),
        vol.Optional("carrier_seen"): vol.Any(None, str),
        vol.Optional("carrier_evidence"): vol.Any(None, str),
        vol.Optional("snapshot"): vol.Any(None, str),
        vol.Optional("collected_at"): vol.Any(None, str),
        vol.Optional("items"): vol.Any(None, str),
    }
)
@websocket_api.async_response
async def ws_order_update(hass: HomeAssistant, connection, msg: dict) -> None:
    """Link an order to a card (card_uid accepts an 8-char prefix), record carrier, etc."""
    from homeassistant.helpers.dispatcher import async_dispatcher_send  # noqa: PLC0415
    from .const import SIGNAL_CHANGED, SIGNAL_ORDERS  # noqa: PLC0415

    data = _data(hass)
    store = data.store
    fields = {k: v for k, v in msg.items() if k not in ("type", "id", "order")}

    def _do():
        if fields.get("card_uid"):
            uid = store.resolve(fields["card_uid"])
            if uid is None:
                raise HomeAssistantError("no unique card for that uid")
            fields["card_uid"] = uid
        return store.order_update(msg["order"], fields)

    try:
        o = await data.run(_do)
    except (HomeAssistantError, ValueError) as err:
        connection.send_error(msg["id"], "failed", str(err))
        return
    async_dispatcher_send(hass, SIGNAL_ORDERS)
    async_dispatcher_send(hass, f"{SIGNAL_CHANGED}_any")
    connection.send_result(msg["id"], o)
