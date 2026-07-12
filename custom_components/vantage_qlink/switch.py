"""Switch platform: enable/disable the controller's internal schedules.

One switch per Vantage TimeControl, created from the imported .qlk
project. ON = the controller executes the schedule (state 0/1);
OFF = time control disabled (state 2). State-only ``VST`` writes were
verified non-destructive on real hardware — the schedule parameters
survive a disable/enable round trip.
"""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .hub import QLinkError, QLinkHub

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(minutes=15)

STATE_DISABLED = 2


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create a schedule switch per imported TimeControl."""
    from . import QLinkRuntime  # avoid circular import

    runtime: QLinkRuntime = hass.data[DOMAIN][entry.entry_id]
    time_controls = runtime.project.get("time_controls") or []
    if not time_controls:
        _LOGGER.debug("No project imported; no schedule switches created")
        return
    async_add_entities(
        (
            QLinkScheduleSwitch(runtime.hub, tc)
            for tc in time_controls
            if tc.get("number") and tc.get("master")
        ),
        update_before_add=True,
    )


class QLinkScheduleSwitch(SwitchEntity):
    """One Vantage internal TimeControl, toggleable from HA."""

    _attr_has_entity_name = False
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, hub: QLinkHub, tc: dict[str, Any]) -> None:
        self._hub = hub
        self._master = int(tc["master"])
        self._number = int(tc["number"])
        self._schedule = str(tc.get("schedule", "") or tc.get("schedule_raw", ""))
        self._state_num: int | None = None
        self._prior_enabled_state = 1  # restored when switching back on
        self._attr_name = f"Vantage Schedule {self._number}"
        self._attr_unique_id = f"vantage_schedule_{self._master}_{self._number}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, "internal_schedules")},
            name="Vantage Internal Schedules",
            manufacturer="Vantage",
            model="Q-Series Time Controls",
        )

    @property
    def available(self) -> bool:
        return self._hub.connected and self._state_num is not None

    @property
    def is_on(self) -> bool | None:
        if self._state_num is None:
            return None
        return self._state_num != STATE_DISABLED

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "master": self._master,
            "function": self._number,
            "schedule": self._schedule,
            "controller_state": self._state_num,
        }

    @staticmethod
    def _parse_state(raw: str) -> int | None:
        """Parse the state field from an RQT/RST reply (or bare form)."""
        tokens = raw.split()
        if tokens and tokens[0].rstrip("#").upper() in ("RQT", "RST"):
            tokens = tokens[3:4]
        else:
            tokens = tokens[0:1]
        try:
            return int(tokens[0])
        except (IndexError, ValueError):
            return None

    async def async_update(self) -> None:
        try:
            raw = await self._hub.get_time_function(self._master, self._number)
        except QLinkError as err:
            _LOGGER.debug("VQT %s/%s failed: %s", self._master, self._number, err)
            return
        state = self._parse_state(raw)
        if state is not None:
            self._state_num = state
            if state != STATE_DISABLED:
                self._prior_enabled_state = state

    async def _set_state(self, state: int) -> None:
        try:
            await self._hub.command(
                "VST",
                self._master,
                self._number,
                state,
                prefixes=("RST",),
                accept_bare=True,
            )
        except QLinkError as err:
            raise HomeAssistantError(
                f"Failed to set schedule {self._number} state: {err}"
            ) from err
        self._state_num = state
        if state != STATE_DISABLED:
            self._prior_enabled_state = state
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set_state(self._prior_enabled_state)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set_state(STATE_DISABLED)
