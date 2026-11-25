"""Mission Control — a SQLite-backed project board for Home Assistant.

The database (/config/mission_control.db) is the source of truth.  Each configured
branch is served as a to-do list (todo.<branch>_tracker) rendered from it, and the
panel at /mission-control reads and writes the store over the WebSocket API.

The optional ``active`` = false mode (set_active service) stops serving the lists so
existing local_todo lists with the same entity ids can be imported first.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from . import shopping_ws
from . import websocket as mc_ws
from .blocks import BlockResolver
from .calendar_sync import CalendarSync
from .const import (
    BACKUP_DIR,
    BRANCHES,
    CONF_ACTIVE,
    CONF_AI_TASK,
    CONF_BRANCHES,
    CONF_CALENDAR,
    CONF_PACKAGES,
    CONF_PERSON,
    CONF_SHOPPING,
    DB_FILE,
    DEFAULT_BRANCHES,
    DOMAIN,
    EVENT_ORDER,
    EVENT_SHOP_QUESTION,
    EXPORT_KEEP,
    PANEL_URL,
    SIGNAL_CHANGED,
    SIGNAL_ORDERS,
    SIGNAL_SHOPPING,
    STATIC_URL,
    VERSION,
    apply_branches,
    entity_id_for,
    set_person_name,
    valid_branch,
)
from .store import MissionStore, StoreError

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.TODO, Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


class MissionData:
    """Runtime handle: the store plus the lock that serializes every call."""

    def __init__(self, hass: HomeAssistant, store: MissionStore) -> None:
        self.hass = hass
        self.store = store
        self.lock = asyncio.Lock()
        self.serving = False
        self.calendar: CalendarSync | None = None
        self.blocks: BlockResolver | None = None
        self.closed = False

    async def run(self, fn: Callable, *args: Any) -> Any:
        """Run a store call in the executor, one at a time."""
        async with self.lock:
            if self.closed:
                # A refresh queued before a reload: the connection is gone.
                raise HomeAssistantError("Mission Control is reloading")
            try:
                return await self.hass.async_add_executor_job(fn, *args)
            except StoreError as err:
                raise HomeAssistantError(str(err)) from err

    def changed(self, *branches: str) -> None:
        for b in {b for b in branches if b}:
            async_dispatcher_send(self.hass, f"{SIGNAL_CHANGED}_{b}")
        # Fired on EVERY write, whichever path wrote it (calendar_sync listens).
        async_dispatcher_send(self.hass, f"{SIGNAL_CHANGED}_any")


type MissionConfigEntry = ConfigEntry[MissionData]


def backup_dir(hass: HomeAssistant) -> Path:
    return Path(hass.config.path(BACKUP_DIR))


PACKAGE_SERVICES = ("order_upsert", "order_update", "orders_list", "prune_door_snapshots")
SHOPPING_SERVICES = ("shopping_add", "shopping_check", "shopping_run", "shopping_answer", "shopping_overview")


async def async_setup_entry(hass: HomeAssistant, entry: MissionConfigEntry) -> bool:
    opts = entry.options
    apply_branches(opts.get(CONF_BRANCHES) or DEFAULT_BRANCHES)
    set_person_name(opts.get(CONF_PERSON))

    store = MissionStore(
        hass.config.path(DB_FILE), dt_util.get_default_time_zone(), dt_util.utcnow
    )
    await hass.async_add_executor_job(store.open)
    data = MissionData(hass, store)
    entry.runtime_data = data

    _register_services(hass)
    for name in (() if opts.get(CONF_PACKAGES) else PACKAGE_SERVICES) + (
        () if opts.get(CONF_SHOPPING) else SHOPPING_SERVICES
    ):
        hass.services.async_remove(DOMAIN, name)
    mc_ws.async_register(hass)
    shopping_ws.async_register(hass)
    await _async_register_panel(hass)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    if opts.get(CONF_ACTIVE, True):
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        data.serving = True
        if opts.get(CONF_CALENDAR):
            data.calendar = CalendarSync(hass, data, opts[CONF_CALENDAR])
            data.calendar.async_start()
        data.blocks = BlockResolver(hass, data)
        data.blocks.async_start()
        if opts.get(CONF_PACKAGES):
            # Door snapshots saved by a carrier-ID automation (/media/packages): keep 30 days.
            from homeassistant.helpers.event import async_track_time_change  # noqa: PLC0415

            async def _nightly_prune(_now) -> None:
                n = await hass.async_add_executor_job(_prune_door_snapshots, hass, SNAPSHOT_KEEP_DAYS)
                _LOGGER.info("Pruned %s door snapshot(s) older than %s days", n, SNAPSHOT_KEEP_DAYS)

            entry.async_on_unload(async_track_time_change(hass, _nightly_prune, hour=3, minute=41, second=0))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: MissionConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def _async_register_panel(hass: HomeAssistant) -> None:
    """Serve the panel's files from the integration folder and add the sidebar entry."""
    from homeassistant.components import frontend, panel_custom  # noqa: PLC0415
    from homeassistant.components.http import StaticPathConfig  # noqa: PLC0415

    if not hass.data.get(f"{DOMAIN}_static"):
        root = Path(__file__).parent / "frontend"
        await hass.http.async_register_static_paths(
            [StaticPathConfig(STATIC_URL, str(root), cache_headers=False)]
        )
        hass.data[f"{DOMAIN}_static"] = True
    if PANEL_URL in hass.data.get(frontend.DATA_PANELS, {}):
        return
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL,
        webcomponent_name="mission-control-panel",
        module_url=f"{STATIC_URL}/mission-control.js?v={VERSION}",
        sidebar_title="Mission Control",
        sidebar_icon="mdi:rocket-launch-outline",
        require_admin=False,
        config={},
    )


SNAPSHOT_KEEP_DAYS = 30


def _prune_door_snapshots(hass: HomeAssistant, days: int) -> int:
    """Delete carrier-ID door snapshots (/media/packages/*.jpg) older than `days`. Returns the count."""
    root = Path(hass.config.media_dirs.get("local", "/media")) / "packages"
    if not root.is_dir():
        return 0
    cutoff = dt_util.utcnow().timestamp() - days * 86400
    n = 0
    for f in root.glob("*.jpg"):
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
                n += 1
        except OSError as err:
            _LOGGER.warning("Could not prune %s: %s", f, err)
    return n


async def async_unload_entry(hass: HomeAssistant, entry: MissionConfigEntry) -> bool:
    ok = True
    if entry.runtime_data.calendar:
        entry.runtime_data.calendar.async_stop()
        entry.runtime_data.calendar = None
    if entry.runtime_data.blocks:
        entry.runtime_data.blocks.async_stop()
        entry.runtime_data.blocks = None
    if entry.runtime_data.serving:
        ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        data = entry.runtime_data
        # Close under the store lock so no in-flight call shares the connection.
        async with data.lock:
            data.closed = True
            await hass.async_add_executor_job(data.store.close)
    return ok


async def async_remove_entry(hass: HomeAssistant, entry: MissionConfigEntry) -> None:
    """Deleting the integration takes the sidebar entry with it (the database stays)."""
    from homeassistant.components import frontend  # noqa: PLC0415

    frontend.async_remove_panel(hass, PANEL_URL)


def get_data(hass: HomeAssistant) -> MissionData:
    entries = [
        e for e in hass.config_entries.async_loaded_entries(DOMAIN)
    ]
    if not entries:
        raise HomeAssistantError("Mission Control is not loaded")
    return entries[0].runtime_data


def get_entry(hass: HomeAssistant) -> MissionConfigEntry:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        raise HomeAssistantError("Mission Control is not loaded")
    return entries[0]


# --------------------------------------------------------------------- services

IMPORT_SCHEMA = vol.Schema(
    {
        vol.Optional("dry_run", default=True): cv.boolean,
        vol.Optional("replace", default=False): cv.boolean,
        vol.Optional("branches"): vol.All(cv.ensure_list, [valid_branch]),
        vol.Optional("force_raw", default=[]): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional("from_file"): cv.string,
    }
)
ACTIVE_SCHEMA = vol.Schema({vol.Required("active"): cv.boolean})
FILE_NOTE_SCHEMA = vol.Schema(
    {
        vol.Required("note"): cv.string,
        vol.Optional("source", default="voice"): cv.string,
        vol.Optional("pins"): vol.Any(None, dict),
        vol.Optional("force", default=False): cv.boolean,
        vol.Optional("dry_run", default=False): cv.boolean,
    }
)
# Package tracker (2026-10-26).
ORDER_UPSERT_SCHEMA = vol.Schema(
    {
        vol.Required("store"): cv.string,
        vol.Optional("order_number"): vol.Any(None, cv.string),
        vol.Optional("tracking_number"): vol.Any(None, cv.string),
        vol.Optional("stage"): vol.Any(None, cv.string),
        vol.Optional("account"): vol.Any(None, cv.string),
        vol.Optional("expected_date"): vol.Any(None, cv.string),
        vol.Optional("expected_window"): vol.Any(None, cv.string),
        vol.Optional("carrier"): vol.Any(None, cv.string),
        vol.Optional("items"): vol.Any(None, cv.string),
        vol.Optional("item_list"): vol.Any(None, cv.string),
        vol.Optional("total"): vol.Any(None, cv.string),
        vol.Optional("order_url"): vol.Any(None, cv.string),
        vol.Optional("message_id"): vol.Any(None, cv.string),
        vol.Optional("received"): vol.Any(None, cv.string),
        vol.Optional("subject"): vol.Any(None, cv.string),
        vol.Optional("source"): cv.string,
    },
    extra=vol.REMOVE_EXTRA,
)
ORDER_UPDATE_SCHEMA = vol.Schema(
    {
        vol.Required("order"): cv.string,
        vol.Optional("card_uid"): vol.Any(None, cv.string),
        vol.Optional("carrier_seen"): vol.Any(None, cv.string),
        vol.Optional("carrier_evidence"): vol.Any(None, cv.string),
        vol.Optional("snapshot"): vol.Any(None, cv.string),
        vol.Optional("collected_at"): vol.Any(None, cv.string),
        vol.Optional("items"): vol.Any(None, cv.string),
    }
)
SHOP_ADD_SCHEMA = vol.Schema(
    {
        vol.Required("name"): cv.string,
        vol.Optional("qty"): vol.Any(None, cv.string),
        vol.Optional("stores"): vol.Any(None, cv.string, [cv.string]),
        vol.Optional("category"): vol.Any(None, cv.string),
        vol.Optional("card_uid"): vol.Any(None, cv.string),
        vol.Optional("note"): vol.Any(None, cv.string),
        vol.Optional("handled"): vol.Any(None, cv.string),
        vol.Optional("needed_by"): vol.Any(None, cv.string),
        vol.Optional("added_by"): vol.Any(None, cv.string),
        vol.Optional("source"): vol.Any(None, cv.string),
    },
    extra=vol.REMOVE_EXTRA,
)
SHOP_CHECK_SCHEMA = vol.Schema(
    {
        vol.Required("item"): cv.string,
        vol.Optional("store"): vol.Any(None, cv.string),
        vol.Optional("price"): vol.Any(None, cv.string, vol.Coerce(float)),
        vol.Optional("source"): cv.string,
    }
)
ORDERS_LIST_SCHEMA = vol.Schema({vol.Optional("recent_days", default=3): vol.Coerce(int)})
STAGE_WORDS = {
    "ordered": "ordered",
    "shipped": "shipped",
    "out_for_delivery": "out for delivery",
    "delivered": "delivered",
    "cancelled": "cancelled",
}


async def _order_changed(hass: HomeAssistant, data: MissionData, res: dict, source: str) -> None:
    """After an orders write: sensors, the stage event, card note, block resolver."""
    async_dispatcher_send(hass, SIGNAL_ORDERS)
    o = res.get("order") or {}
    if res.get("stage_changed"):
        hass.bus.async_fire(
            EVENT_ORDER,
            {"prev_stage": res.get("prev_stage"), "source": source,
             **{k: v for k, v in o.items() if k != "events"}},
        )
        card = o.get("card_uid")
        if card:
            word = STAGE_WORDS.get(o.get("stage"), o.get("stage"))
            when = ""
            if o.get("stage") in ("ordered", "shipped") and o.get("expected_date"):
                when = f", arriving {o['expected_date']}" + (
                    f" {o['expected_window']}" if o.get("expected_window") else "")
            via = f" via {o['carrier']}" if o.get("carrier") and o.get("stage") != "ordered" else ""
            text = f"order {o.get('order_number') or o.get('tracking')} ({o.get('store')}) {word}{via}{when}"
            try:
                branch = await data.run(data.store.add_note, card, text, None, "system:orders")
                data.changed(branch)
            except Exception:  # noqa: BLE001
                _LOGGER.warning("could not note order %s on card %s", o.get("key"), card)
    # Shopping list (2026-10-29): linked list items moved with the order.
    sh = res.get("shopping") or {}
    if sh.get("changed") or sh.get("linked"):
        async_dispatcher_send(hass, SIGNAL_SHOPPING)
    if sh.get("question"):
        q = sh["question"]
        hass.bus.async_fire(
            EVENT_SHOP_QUESTION,
            {"question": q["id"], "order_key": q["order_key"], "store": o.get("store"),
             "order_items": o.get("items"), "total": o.get("total"),
             "candidates": q["candidates"]},
        )
    # Let the block resolver look at "order …" text blocks.
    async_dispatcher_send(hass, f"{SIGNAL_CHANGED}_any")


async def shopping_changed(hass: HomeAssistant, data: MissionData, card_uid: str | None = None) -> None:
    """After a shopping write: sensor + list refresh; the card's list if one was touched."""
    async_dispatcher_send(hass, SIGNAL_SHOPPING)
    if card_uid:
        row = await data.run(data.store.get, card_uid)
        data.changed(row["branch"] if row else None)


def _reg(hass: HomeAssistant, domain: str, name: str, handler, **kwargs) -> None:
    if not hass.services.has_service(domain, name):
        hass.services.async_register(domain, name, handler, **kwargs)


def _register_services(hass: HomeAssistant) -> None:

    async def import_from_todo(call: ServiceCall) -> ServiceResponse:
        return await _async_import(hass, call.data)

    async def export(call: ServiceCall) -> ServiceResponse:
        return await _async_export(hass)

    async def set_active(call: ServiceCall) -> ServiceResponse:
        return await _async_set_active(hass, call.data["active"])

    _reg(hass, 
        DOMAIN,
        "import_from_todo",
        import_from_todo,
        schema=IMPORT_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    _reg(hass, 
        DOMAIN, "export", export, supports_response=SupportsResponse.OPTIONAL
    )
    async def order_upsert(call: ServiceCall) -> ServiceResponse:
        data = get_data(hass)
        payload = dict(call.data)
        try:
            res = await data.run(data.store.order_upsert, payload)
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err
        await _order_changed(hass, data, res, payload.get("source", "email"))
        o = res["order"] or {}
        return {"changed": res["changed"], "stage_changed": res["stage_changed"],
                "prev_stage": res["prev_stage"],
                "order": {k: v for k, v in o.items() if k != "events"}}

    async def prune_door_snapshots(call: ServiceCall) -> ServiceResponse:
        days = call.data.get("days", SNAPSHOT_KEEP_DAYS)
        n = await hass.async_add_executor_job(_prune_door_snapshots, hass, days)
        return {"deleted": n, "days": days}

    _reg(hass, 
        DOMAIN, "prune_door_snapshots", prune_door_snapshots,
        schema=vol.Schema({vol.Optional("days", default=SNAPSHOT_KEEP_DAYS): vol.All(vol.Coerce(int), vol.Range(min=7))}),
        supports_response=SupportsResponse.ONLY,
    )

    async def order_update(call: ServiceCall) -> ServiceResponse:
        data = get_data(hass)
        fields = {k: v for k, v in call.data.items() if k != "order"}

        def _do():
            if fields.get("card_uid"):
                uid = data.store.resolve(fields["card_uid"])
                if uid is None:
                    raise ValueError("no unique card for that uid")
                fields["card_uid"] = uid
            return data.store.order_update(call.data["order"], fields)

        try:
            o = await data.run(_do)
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err
        async_dispatcher_send(hass, SIGNAL_ORDERS)
        return {"order": {k: v for k, v in o.items() if k != "events"}}

    async def orders_list(call: ServiceCall) -> ServiceResponse:
        data = get_data(hass)
        return await data.run(data.store.order_listing, call.data["recent_days"])

    # ---------------------------------------------------- shopping (2026-10-29)
    async def shopping_add(call: ServiceCall) -> ServiceResponse:
        data = get_data(hass)
        try:
            res = await data.run(data.store.shop_add, dict(call.data))
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err
        await shopping_changed(hass, data, res["item"].get("card_uid"))
        return {"merged": res["merged"], "item": res["item"]}

    async def shopping_check(call: ServiceCall) -> ServiceResponse:
        data = get_data(hass)
        try:
            it = await data.run(data.store.shop_check, call.data["item"], call.data.get("store"),
                                call.data.get("price"), call.data.get("source", "check"))
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err
        await shopping_changed(hass, data, it.get("card_uid"))
        return {"item": it}

    async def shopping_run(call: ServiceCall) -> ServiceResponse:
        data = get_data(hass)
        return await data.run(data.store.shop_run, call.data["store"])

    async def shopping_answer(call: ServiceCall) -> ServiceResponse:
        data = get_data(hass)
        item = call.data.get("item") or None
        if item in ("none", "None"):
            item = None
        try:
            res = await data.run(data.store.shop_answer, call.data["question"], item)
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err
        await shopping_changed(hass, data)
        async_dispatcher_send(hass, SIGNAL_ORDERS)
        return res

    async def shopping_overview(call: ServiceCall) -> ServiceResponse:
        data = get_data(hass)
        return await data.run(data.store.shop_overview)

    _reg(hass, 
        DOMAIN, "shopping_add", shopping_add, schema=SHOP_ADD_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    _reg(hass, 
        DOMAIN, "shopping_check", shopping_check, schema=SHOP_CHECK_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    _reg(hass, 
        DOMAIN, "shopping_run", shopping_run, schema=vol.Schema({vol.Required("store"): cv.string}),
        supports_response=SupportsResponse.ONLY,
    )
    _reg(hass, 
        DOMAIN, "shopping_answer", shopping_answer,
        schema=vol.Schema({vol.Required("question"): cv.string,
                           vol.Optional("item"): vol.Any(None, cv.string)}),
        supports_response=SupportsResponse.OPTIONAL,
    )
    _reg(hass, 
        DOMAIN, "shopping_overview", shopping_overview, schema=vol.Schema({}),
        supports_response=SupportsResponse.ONLY,
    )
    _reg(hass, 
        DOMAIN, "order_upsert", order_upsert, schema=ORDER_UPSERT_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    _reg(hass, 
        DOMAIN, "order_update", order_update, schema=ORDER_UPDATE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    _reg(hass, 
        DOMAIN, "orders_list", orders_list, schema=ORDERS_LIST_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    async def file_note(call: ServiceCall) -> ServiceResponse:
        from .quickadd import async_file_note  # noqa: PLC0415

        data = get_data(hass)
        entry = get_entry(hass)
        try:
            return await async_file_note(
                hass, data, entry.options.get(CONF_AI_TASK), call.data["note"],
                call.data.get("source", "voice"), call.data.get("pins"),
                call.data.get("force", False), call.data.get("dry_run", False),
            )
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err

    async def digest(call: ServiceCall) -> ServiceResponse:
        from .digest import async_build  # noqa: PLC0415

        data = get_data(hass)
        o = get_entry(hass).options
        return await async_build(
            hass, data, call.data["kind"], o.get(CONF_CALENDAR),
            bool(o.get(CONF_PACKAGES)), bool(o.get(CONF_SHOPPING)),
        )

    _reg(hass, 
        DOMAIN, "file_note", file_note, schema=FILE_NOTE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    _reg(hass, 
        DOMAIN, "digest", digest,
        schema=vol.Schema({vol.Required("kind"): vol.In(["morning", "evening"])}),
        supports_response=SupportsResponse.ONLY,
    )
    _reg(hass, 
        DOMAIN,
        "set_active",
        set_active,
        schema=ACTIVE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )


def _jsonable_item(item: dict) -> dict:
    out = {}
    for k in ("uid", "summary", "status", "due", "description", "completed"):
        v = item.get(k)
        if isinstance(v, (dt.date, dt.datetime)):
            v = v.isoformat()
        out[k] = v
    return out


async def _async_import(hass: HomeAssistant, opts: dict) -> dict:
    data = get_data(hass)
    entry = get_entry(hass)
    branches = opts.get("branches") or list(BRANCHES)
    ent_reg = er.async_get(hass)

    source: dict[str, list[dict]] = {}
    if opts.get("from_file"):
        path = Path(opts["from_file"])
        if backup_dir(hass).resolve() not in path.resolve().parents:
            raise HomeAssistantError(f"from_file must live under /config/{BACKUP_DIR}/")
        raw = await hass.async_add_executor_job(path.read_text, "utf-8")
        blob = json.loads(raw)
        lists = blob.get("todo_lists", blob)
        for b in branches:
            source[b] = [_jsonable_item(i) for i in lists.get(b, [])]
    else:
        for b in branches:
            eid = entity_id_for(b)
            reg = ent_reg.async_get(eid)
            if reg is not None and reg.platform == DOMAIN:
                raise HomeAssistantError(
                    f"{eid} is already served by Mission Control; "
                    "import reads the local_todo lists before cutover"
                )
            if hass.states.get(eid) is None:
                raise HomeAssistantError(f"{eid} is not available to read")
            resp = await hass.services.async_call(
                "todo",
                "get_items",
                {"entity_id": eid},
                blocking=True,
                return_response=True,
            )
            source[b] = [_jsonable_item(i) for i in resp[eid]["items"]]

    store = data.store
    rows, report = await data.run(store.plan_import, source, opts.get("force_raw", []))

    # Round-trip proof on the plan: every lean row must project back to the
    # text it came from (exact) or to the listed normalized text.
    src_by_uid = {i["uid"]: i for items in source.values() for i in items}
    mismatches = []
    for row, notes in rows:
        projected = store.describe({**row, "notes": notes})
        orig = src_by_uid[row["uid"]].get("description")
        if row["format"] == "raw" and projected != (orig or None):
            mismatches.append(row["uid"])
    report["raw_roundtrip_mismatches"] = mismatches
    report["dry_run"] = opts.get("dry_run", True)

    stamp = dt_util.now().strftime("%Y%m%d-%H%M%S")
    bdir = backup_dir(hass)
    await hass.async_add_executor_job(lambda: bdir.mkdir(exist_ok=True))
    if report["errors"] or mismatches:
        report["committed"] = False
    elif not report["dry_run"]:
        if entry.options.get(CONF_ACTIVE, True):
            raise HomeAssistantError("set_active false before re-importing")
        await data.run(store.import_rows, rows, branches, opts.get("replace", False))
        # Read-back: every item from the store must project to what was planned.
        back = {r["uid"]: r for r in await data.run(store.items)}
        bad = []
        for row, notes in rows:
            got = back.get(row["uid"])
            want = store.describe({**row, "notes": notes})
            if got is None or store.describe(got) != want or got["title"] != row["title"]:
                bad.append(row["uid"])
        report["readback_mismatches"] = bad
        report["committed"] = True
    else:
        report["committed"] = False

    path = bdir / f"phase2_import_report_{stamp}.json"
    payload = json.dumps(
        {"report": report, "source": source}, ensure_ascii=False, indent=1
    )
    await hass.async_add_executor_job(path.write_text, payload, "utf-8")
    report["report_path"] = str(path)
    # Keep the service response small: diffs live in the report file.
    return {
        "dry_run": report["dry_run"],
        "committed": report["committed"],
        "branches": report["branches"],
        "normalized": [
            {k: e[k] for k in ("branch", "uid", "status", "title")}
            for e in report["normalized"]
        ],
        "raw": [
            {k: e[k] for k in ("branch", "uid", "status", "title")}
            for e in report["raw"]
        ],
        "errors": report["errors"],
        "raw_roundtrip_mismatches": mismatches,
        "readback_mismatches": report.get("readback_mismatches", []),
        "report_path": report["report_path"],
    }


async def _async_export(hass: HomeAssistant) -> dict:
    data = get_data(hass)
    blob = await data.run(data.store.export)
    bdir = backup_dir(hass)
    stamp = dt_util.now().strftime("%Y%m%d-%H%M%S")
    path = bdir / f"mission_control_export_{stamp}.json"

    def _write() -> int:
        bdir.mkdir(exist_ok=True)
        path.write_text(json.dumps(blob, ensure_ascii=False, indent=1), "utf-8")
        old = sorted(bdir.glob("mission_control_export_*.json"))
        for f in old[:-EXPORT_KEEP]:
            f.unlink(missing_ok=True)
        return path.stat().st_size

    size = await hass.async_add_executor_job(_write)
    return {
        "path": str(path),
        "bytes": size,
        "items": len(blob["items"]),
        "per_branch": {b: len(v) for b, v in blob["todo_lists"].items()},
    }


async def _async_set_active(hass: HomeAssistant, active: bool) -> dict:
    entry = get_entry(hass)
    ent_reg = er.async_get(hass)
    if active:
        clash = []
        for b in BRANCHES:
            reg = ent_reg.async_get(entity_id_for(b))
            if reg is not None and reg.platform != DOMAIN:
                clash.append(f"{reg.entity_id} ({reg.platform}, disabled_by={reg.disabled_by})")
        if clash:
            raise HomeAssistantError(
                "entity ids still held by another integration — rename them away "
                "first: " + "; ".join(clash)
            )
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_ACTIVE: active}
    )
    await hass.config_entries.async_reload(entry.entry_id)
    if not active:
        # Free the entity ids so the local_todo lists can take them back.
        for reg in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
            ent_reg.async_remove(reg.entity_id)
    served = sorted(
        r.entity_id for r in er.async_entries_for_config_entry(ent_reg, entry.entry_id)
    )
    return {"active": active, "entities": served}
