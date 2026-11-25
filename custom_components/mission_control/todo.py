"""The eight branch trackers as to-do lists — a projection of the database."""

from __future__ import annotations

import datetime as dt
import logging

from homeassistant.components import persistent_notification
from homeassistant.components.todo import (
    TodoItem,
    TodoItemStatus,
    TodoListEntity,
    TodoListEntityFeature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import MissionConfigEntry, MissionData
from .const import BRANCHES, CONF_SHOPPING, DOMAIN, SHOPPING_TODO, SIGNAL_CHANGED, SIGNAL_SHOPPING, entity_id_for
from .websocket import owner_of

# Derived, read-only lists (2026-10-08): a query over all 8
# branches projected as a to-do list, so the phone shows the board's
# "Ready for you" column and the week's dates without a second copy of any card.
VIEWS = {
    "ready_for_you": ("Mission Ready for You", "todo.mission_ready_for_you"),
    "this_week": ("Mission This Week", "todo.mission_this_week"),
    "today": ("Mission Today", "todo.mission_today"),  # docs/CARD_FORMAT.md (Today rank), 2026-11-01
}

_LOGGER = logging.getLogger(__name__)


def due_to_str(due: dt.date | dt.datetime | None) -> str | None:
    if due is None:
        return None
    return due.isoformat()


def str_to_due(value: str | None) -> dt.date | dt.datetime | None:
    if not value:
        return None
    if len(value) == 10:
        return dt.date.fromisoformat(value)
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_util.get_default_time_zone())
    return parsed


def str_to_dt(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MissionConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    ent_reg = er.async_get(hass)
    # A branch removed in the options leaves its list behind in the registry: drop it.
    keep = {f"{DOMAIN}_{b}" for b in BRANCHES}
    for reg in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
        uid = reg.unique_id or ""
        if (reg.domain == "todo" and uid.startswith(f"{DOMAIN}_") and uid not in keep
                and not uid.startswith(f"{DOMAIN}_view_") and uid != f"{DOMAIN}_shopping_list"):
            ent_reg.async_remove(reg.entity_id)
    entities = []
    clash = []
    for branch, name in BRANCHES.items():
        reg = ent_reg.async_get(entity_id_for(branch))
        if reg is not None and reg.platform != DOMAIN:
            clash.append(reg.entity_id)
            continue
        entities.append(MissionTodoList(entry.runtime_data, branch, f"{name} Tracker"))
    if clash:
        msg = (
            "Mission Control did not serve "
            + ", ".join(clash)
            + " because another integration still holds those entity ids "
            "(a second copy would get a _2 id). Rename the old entities away, "
            "then reload Mission Control."
        )
        _LOGGER.error(msg)
        persistent_notification.async_create(
            hass, msg, "Mission Control", f"{DOMAIN}_clash"
        )
    for view, (name, entity_id) in VIEWS.items():
        entities.append(MissionViewList(entry.runtime_data, view, name, entity_id))
    # Shopping list (optional module): its own to-do list, next to HA's built-in one.
    if not entry.options.get(CONF_SHOPPING):
        old = ent_reg.async_get_entity_id("todo", DOMAIN, f"{DOMAIN}_shopping_list")
        if old:
            ent_reg.async_remove(old)
    else:
        reg = ent_reg.async_get(SHOPPING_TODO)
        if reg is not None and reg.platform != DOMAIN:
            _LOGGER.warning("%s is held by %s; rename that entity to serve the shopping list",
                            SHOPPING_TODO, reg.platform)
        else:
            entities.append(ShoppingTodoList(entry.runtime_data))
    async_add_entities(entities)




class ShoppingTodoList(TodoListEntity):
    """The house shopping list as a to-do list (phone, Assist, the companion app).

    Since 2026-11-02 this is the *to-buy* view: open = needed items that nobody has
    handled yet; checked = bought (shown for 2 days).  Items already ordered or on
    their way are left out — they live on the Packages page and the Shopping page's
    "Already ordered" fold.  The due date is the item's needed-by (its own or the
    linked card's); setting one on the phone sets the item's own needed_by.  Adding
    an item here goes through the same merge + category guess as everywhere else;
    deleting dismisses it.  Stores, profiles and card links are edited on the page.
    """

    _attr_has_entity_name = False
    _attr_should_poll = False
    _attr_supported_features = (
        TodoListEntityFeature.CREATE_TODO_ITEM
        | TodoListEntityFeature.DELETE_TODO_ITEM
        | TodoListEntityFeature.UPDATE_TODO_ITEM
        | TodoListEntityFeature.SET_DESCRIPTION_ON_ITEM
        | TodoListEntityFeature.SET_DUE_DATE_ON_ITEM
    )

    def __init__(self, data: MissionData) -> None:
        self._data = data
        self._attr_name = "Mission shopping"
        self._attr_unique_id = f"{DOMAIN}_shopping_list"
        self.entity_id = SHOPPING_TODO

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_SHOPPING, self._on_change)
        )
        await self._refresh()

    @callback
    def _on_change(self) -> None:
        self.hass.async_create_task(self._refresh(write=True))

    async def _refresh(self, write: bool = False) -> None:
        ov = await self._data.run(self._data.store.shop_overview)
        names = {s["key"]: s["name"] for s in ov["stores"]}
        out = []
        for i in ov["items"]:
            if i["status"] in ("ordered", "arriving") or (i["status"] == "needed" and i.get("handled")):
                continue  # not something to go and buy
            bits = []
            if i.get("stores"):
                bits.append(" / ".join(names.get(k, k) for k in i["stores"]))
            if (i.get("card") or {}).get("title"):
                bits.append("for: " + i["card"]["title"])
            if i.get("note"):
                bits.append(i["note"])
            due = None
            if i.get("needed_by_eff"):
                try:
                    due = dt.date.fromisoformat(i["needed_by_eff"])
                except ValueError:
                    due = None
            out.append(TodoItem(
                uid=i["id"],
                summary=i["name"] + (f" ×{i['qty']}" if i.get("qty") else ""),
                status=TodoItemStatus.COMPLETED if i["status"] == "done" else TodoItemStatus.NEEDS_ACTION,
                description=" · ".join(bits) or None,
                due=due,
            ))
        self._attr_todo_items = out
        if write:
            self.async_write_ha_state()

    async def _after(self) -> None:
        from homeassistant.helpers.dispatcher import async_dispatcher_send  # noqa: PLC0415

        async_dispatcher_send(self.hass, SIGNAL_SHOPPING)

    async def async_create_todo_item(self, item: TodoItem) -> None:
        name, qty = _split_qty(item.summary or "")
        await self._data.run(self._data.store.shop_add, {
            "name": name, "qty": qty, "note": item.description, "source": "todo"})
        await self._after()

    async def async_update_todo_item(self, item: TodoItem) -> None:
        cur = await self._data.run(self._data.store.shop_get, item.uid)
        if cur is None:
            return
        store = self._data.store
        if item.status == TodoItemStatus.COMPLETED and cur["status"] != "done":
            await self._data.run(store.shop_check, cur["id"], None, None, "todo")
        elif item.status == TodoItemStatus.NEEDS_ACTION and cur["status"] == "done":
            await self._data.run(store.shop_uncheck, cur["id"])
        shown = cur["name"] + (f" ×{cur['qty']}" if cur.get("qty") else "")
        if item.summary and item.summary.strip() != shown:
            name, qty = _split_qty(item.summary)
            await self._data.run(store.shop_update, cur["id"], {"name": name, "qty": qty})
        # a date set or cleared on the phone is the item's own needed_by (a card's due
        # still shows when the item has none of its own)
        new_due = item.due.isoformat() if isinstance(item.due, (dt.date, dt.datetime)) else None
        if new_due != cur.get("needed_by") and (new_due or cur.get("needed_by")):
            await self._data.run(store.shop_update, cur["id"], {"needed_by": new_due})
        await self._after()

    async def async_delete_todo_items(self, uids: list[str]) -> None:
        for uid in uids:
            await self._data.run(self._data.store.shop_update, uid, {"status": "dismissed"})
        await self._after()


def _split_qty(summary: str) -> tuple[str, str | None]:
    """"Dish soap ×2", "dish soap x2", "2 dish soap" -> ("Dish soap", "2")."""
    import re  # noqa: PLC0415

    s = summary.strip()
    m = re.match(r"^(.*?)\s*×\s*(.+?)\s*$", s) or re.match(r"^(.*?)\s+[xX]\s*(\d+)\s*$", s)
    if m and m.group(1):
        return m.group(1).strip(), m.group(2)
    m = re.match(r"^(\d+)\s+(.+)$", s)
    if m:
        return m.group(2).strip(), m.group(1)
    return s, None


class MissionViewList(TodoListEntity):
    """A query over the whole store shown as a list; only status can change."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_supported_features = TodoListEntityFeature.UPDATE_TODO_ITEM

    def __init__(self, data: MissionData, view: str, name: str, entity_id: str) -> None:
        self._data = data
        self._view = view
        self._attr_name = name
        self._attr_unique_id = f"{DOMAIN}_view_{view}"
        self.entity_id = entity_id

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(self.hass, f"{SIGNAL_CHANGED}_any", self._on_change)
        )
        await self._refresh()

    @callback
    def _on_change(self) -> None:
        self.hass.async_create_task(self._refresh(write=True))

    async def _refresh(self, write: bool = False) -> None:
        store = self._data.store
        rows = await self._data.run(store.view_rows, self._view, owner_of)
        self._attr_todo_items = [
            TodoItem(
                uid=r["uid"],
                summary=r["title"],
                status=TodoItemStatus.NEEDS_ACTION,
                due=str_to_due(r["due"]),
                description=f"{BRANCHES.get(r['branch'], r['branch'])} · {r.get('next_step') or ''}".rstrip(" ·"),
            )
            for r in rows
        ]
        if write:
            self.async_write_ha_state()

    async def async_update_todo_item(self, item: TodoItem) -> None:
        """Checking an item off closes the home card; nothing else is editable here."""
        if item.status is None:
            return
        uid, (old_b, new_b) = await self._data.run(
            self._update, item.uid, str(item.status)
        )
        await self._refresh()
        self.async_write_ha_state()
        self._data.changed(old_b, new_b)

    def _update(self, uid: str, status: str):
        return uid, self._data.store.update_fields(uid, {"status": status})


class MissionTodoList(TodoListEntity):
    """One branch tracker, rendered from the Mission Control database."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_supported_features = (
        TodoListEntityFeature.CREATE_TODO_ITEM
        | TodoListEntityFeature.DELETE_TODO_ITEM
        | TodoListEntityFeature.UPDATE_TODO_ITEM
        | TodoListEntityFeature.MOVE_TODO_ITEM
        | TodoListEntityFeature.SET_DUE_DATE_ON_ITEM
        | TodoListEntityFeature.SET_DUE_DATETIME_ON_ITEM
        | TodoListEntityFeature.SET_DESCRIPTION_ON_ITEM
    )

    def __init__(self, data: MissionData, branch: str, name: str) -> None:
        self._data = data
        self._branch = branch
        self._attr_name = name
        self._attr_unique_id = f"{DOMAIN}_{branch}"
        self.entity_id = entity_id_for(branch)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, f"{SIGNAL_CHANGED}_{self._branch}", self._on_change
            )
        )
        await self._refresh()

    @callback
    def _on_change(self) -> None:
        self.hass.async_create_task(self._refresh(write=True))

    async def _refresh(self, write: bool = False) -> None:
        store = self._data.store
        # Archive window: old closed cards stay out of the list.
        rows = await self._data.run(store.projected, self._branch)
        self._attr_todo_items = [
            TodoItem(
                uid=r["uid"],
                summary=r["title"],
                status=TodoItemStatus(r["status"]),
                due=str_to_due(r["due"]),
                description=store.describe(r),
                completed=str_to_dt(r["completed_at"])
                if r["status"] == "completed"
                else None,
            )
            for r in rows
        ]
        if write:
            self.async_write_ha_state()

    async def _commit(self, *branches: str) -> None:
        await self._refresh()
        self.async_write_ha_state()
        self._data.changed(*(b for b in branches if b != self._branch))

    async def async_create_todo_item(self, item: TodoItem) -> None:
        await self._data.run(
            self._data.store.create,
            self._branch,
            item.summary or "",
            str(item.status or TodoItemStatus.NEEDS_ACTION),
            due_to_str(item.due),
            item.description,
            None,
            None,
        )
        await self._commit()

    async def async_update_todo_item(self, item: TodoItem) -> None:
        await self._data.run(
            self._data.store.update_from_todo,
            item.uid,
            item.summary,
            str(item.status) if item.status else None,
            due_to_str(item.due),
            item.description,
            item.completed.isoformat(timespec="seconds") if item.completed else None,
        )
        await self._commit()

    async def async_delete_todo_items(self, uids: list[str]) -> None:
        await self._data.run(self._data.store.delete, uids)
        await self._commit()

    async def async_move_todo_item(
        self, uid: str, previous_uid: str | None = None
    ) -> None:
        if uid == previous_uid:
            return
        await self._data.run(self._data.store.move, self._branch, uid, previous_uid)
        await self._commit()
