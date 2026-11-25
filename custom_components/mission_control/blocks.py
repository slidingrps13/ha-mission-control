"""Auto-unblock (2026-10-08).

Runs ``MissionStore.resolve_blocks`` a few seconds after any store write (so a
card blocked by another card comes free the moment that card closes) and once a
day at 00:05 house time (so date blocks clear the morning they pass).  Freed
cards get a dated ``unblocked: …`` note from the store; this module only fires
the change signal for their branches and logs what came free.  Text blocks are
never cleared here — only a person can, through ``mission_control/blocks/clear``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_track_time_change

from .const import SIGNAL_CHANGED

if TYPE_CHECKING:
    from . import MissionData

_LOGGER = logging.getLogger(__name__)
DEBOUNCE_SECONDS = 3


class BlockResolver:
    def __init__(self, hass: HomeAssistant, data: MissionData) -> None:
        self.hass = hass
        self.data = data
        self._timer: asyncio.TimerHandle | None = None
        self._unsub: list = []
        self._running = False
        self._pending = False

    @callback
    def async_start(self) -> None:
        self._unsub.append(
            async_dispatcher_connect(self.hass, f"{SIGNAL_CHANGED}_any", self._schedule)
        )
        self._unsub.append(
            async_track_time_change(self.hass, self._daily, hour=0, minute=5, second=0)
        )
        self._schedule(delay=20)  # first pass after start

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
            delay, lambda: self.hass.async_create_task(self.run())
        )

    async def _daily(self, _now: dt.datetime) -> None:
        await self.run()

    async def run(self) -> list[dict]:
        if self._running:
            self._pending = True
            return []
        self._running = True
        try:
            freed = await self.data.run(self.data.store.resolve_blocks)
            if freed:
                for f in freed:
                    _LOGGER.info(
                        "unblocked %s (%s): %s", f["uid"][:8], f["title"], "; ".join(f["reasons"])
                    )
                self.data.changed(*{f["branch"] for f in freed})
            return freed
        except Exception as err:  # noqa: BLE001 — never break the writer
            _LOGGER.warning("block resolver: %s", err)
            return []
        finally:
            self._running = False
            if self._pending:
                self._pending = False
                self._schedule()
