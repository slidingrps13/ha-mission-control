"""Card due dates → calendar events (optional; set a calendar in the options).

One direction only: every OPEN card with a ``due`` gets one event on the chosen
calendar; the event moves when the due date or title changes and disappears
when the card closes or loses its due date.  Events made by hand on the
calendar are never touched — only events this
module created (tracked in the ``calendar_events`` table) are updated or
deleted.

The reconcile runs 5 s after any store write (``SIGNAL_CHANGED_any``) and every
``RECONCILE_MINUTES`` as a safety net.  It never raises into the writer: a
calendar failure is logged and retried on the next pass.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import BRANCHES, SIGNAL_CHANGED

if TYPE_CHECKING:
    from . import MissionData

_LOGGER = logging.getLogger(__name__)

DEBOUNCE_SECONDS = 5
RECONCILE_MINUTES = 15
TAG = "Mission Control card"  # description marker; voice_today and the panel skip these


def event_description(item: dict) -> str:
    """The description every synced event carries (the marker line first)."""
    lines = [f"{TAG} {item['uid'][:8]} ({BRANCHES.get(item['branch'], item['branch'])})"]
    if item.get("next_step"):
        lines.append(item["next_step"])
    return "\n".join(lines)


def event_window(due: str) -> tuple[Any, Any, bool]:
    """(dtstart, dtend, all_day) for a card ``due`` value.

    A bare date is an all-day event; a date-time gets a one-hour slot.
    """
    due = (due or "").strip()
    if len(due) <= 10:
        d = dt.date.fromisoformat(due[:10])
        return d, d + dt.timedelta(days=1), True
    start = dt.datetime.fromisoformat(due)
    if start.tzinfo is None:
        start = start.replace(tzinfo=dt_util.get_default_time_zone())
    return start, start + dt.timedelta(hours=1), False


class CalendarSync:
    """Keeps the house calendar in step with the cards' due dates."""

    def __init__(self, hass: HomeAssistant, data: MissionData, calendar_entity: str) -> None:
        self.hass = hass
        self.data = data
        self.calendar_entity = calendar_entity
        self._timer: asyncio.TimerHandle | None = None
        self._unsub: list = []
        self._running = False
        self._pending = False
        self._warned_missing = False

    # ------------------------------------------------------------- lifecycle
    @callback
    def async_start(self) -> None:
        self._unsub.append(
            async_dispatcher_connect(self.hass, f"{SIGNAL_CHANGED}_any", self._schedule)
        )
        self._unsub.append(
            async_track_time_interval(
                self.hass, self._interval, dt.timedelta(minutes=RECONCILE_MINUTES)
            )
        )
        # First pass shortly after start, once the calendar entity is up.
        self._schedule(delay=30)

    @callback
    def async_stop(self) -> None:
        for u in self._unsub:
            u()
        self._unsub.clear()
        if self._timer:
            self._timer.cancel()
            self._timer = None

    @callback
    def _schedule(self, delay: float = DEBOUNCE_SECONDS) -> None:
        if self._timer:
            self._timer.cancel()
        self._timer = self.hass.loop.call_later(
            delay, lambda: self.hass.async_create_task(self.reconcile())
        )

    async def _interval(self, _now: dt.datetime) -> None:
        await self.reconcile()

    # ------------------------------------------------------------- the work
    def _entity(self):
        component = self.hass.data.get("calendar")
        ent = component.get_entity(self.calendar_entity) if component else None
        if ent is None and not self._warned_missing:
            _LOGGER.warning("calendar sync: %s is not available yet", self.calendar_entity)
            self._warned_missing = True
        return ent

    async def reconcile(self) -> dict[str, int]:
        """Create / move / delete events so they match the open cards' due dates."""
        if self._running:
            self._pending = True
            return {}
        self._running = True
        stats = {"created": 0, "moved": 0, "deleted": 0, "errors": 0}
        try:
            ent = self._entity()
            if ent is None:
                return stats
            desired = {
                it["uid"]: it for it in await self.data.run(self.data.store.due_items)
            }
            tracked = await self.data.run(self.data.store.calendar_rows)

            for uid, item in desired.items():
                row = tracked.get(uid)
                if row and row["due"] == item["due"] and row["title"] == item["title"]:
                    continue
                try:
                    if row:
                        await self._delete(ent, row["event_uid"])
                    event_uid = await self._create(ent, item)
                    await self.data.run(
                        self.data.store.calendar_set,
                        uid,
                        event_uid,
                        item["due"],
                        item["title"],
                    )
                    stats["moved" if row else "created"] += 1
                except Exception as err:  # noqa: BLE001 — never break the writer
                    stats["errors"] += 1
                    _LOGGER.warning("calendar sync: card %s: %s", uid[:8], err)

            for uid, row in tracked.items():
                if uid in desired:
                    continue
                try:
                    await self._delete(ent, row["event_uid"])
                    await self.data.run(self.data.store.calendar_del, uid)
                    stats["deleted"] += 1
                except Exception as err:  # noqa: BLE001
                    stats["errors"] += 1
                    _LOGGER.warning("calendar sync: removing %s: %s", uid[:8], err)
            if any(stats.values()):
                _LOGGER.info("calendar sync: %s", stats)
            return stats
        finally:
            self._running = False
            if self._pending:
                self._pending = False
                self._schedule()

    async def _create(self, ent, item: dict) -> str:
        start, end, all_day = event_window(item["due"])
        await ent.async_create_event(
            summary=item["title"],
            description=event_description(item),
            dtstart=start,
            dtend=end,
        )
        # Google's create returns nothing: find the event we just made by its marker.
        marker = f"{TAG} {item['uid'][:8]}"
        if all_day:
            s = dt.datetime.combine(start, dt.time.min, dt_util.get_default_time_zone())
            e = s + dt.timedelta(days=1)
        else:
            s, e = start - dt.timedelta(minutes=1), end + dt.timedelta(minutes=1)
        for ev in await ent.async_get_events(self.hass, s, e):
            if marker in (ev.description or "") and ev.uid:
                return ev.uid
        raise RuntimeError("event created but not found on read-back")

    async def _delete(self, ent, event_uid: str | None) -> None:
        if not event_uid:
            return
        try:
            await ent.async_delete_event(event_uid)
        except Exception as err:  # noqa: BLE001 — already gone is fine
            _LOGGER.debug("calendar sync: delete %s: %s", event_uid, err)
