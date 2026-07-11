"""Cover platform for Vantage QLink loads driving shades/covers."""

from __future__ import annotations

from typing import Any

from homeassistant.components.cover import (
    ATTR_POSITION,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_COVERS, DOMAIN
from .coordinator import QLinkCoordinator
from .hub import QLinkError


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the cover platform from the configured contractor numbers."""
    from . import QLinkRuntime, parse_id_list  # avoid circular import

    runtime: QLinkRuntime = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        QLinkCover(runtime.coordinator, con)
        for con in parse_id_list(entry.options.get(CONF_COVERS))
    )


class QLinkCover(CoordinatorEntity[QLinkCoordinator], CoverEntity):
    """A Vantage load exposed as a positional cover."""

    _attr_supported_features = (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.SET_POSITION
    )

    def __init__(self, coordinator: QLinkCoordinator, contractor_number: int) -> None:
        super().__init__(coordinator)
        self._con = contractor_number
        self._attr_unique_id = f"vantage_cover_{contractor_number}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{contractor_number}")},
            name=f"Load {contractor_number}",
            manufacturer="Vantage",
            model="Load",
            serial_number=f"{contractor_number}",
        )

    @property
    def _level(self) -> int:
        data = self.coordinator.data or {}
        return int(data.get(self._con, 0))

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.hub.connected

    @property
    def is_closed(self) -> bool:
        return self._level == 0

    @property
    def current_cover_position(self) -> int:
        return self._level

    async def _async_set_level(self, level: int) -> None:
        try:
            await self.coordinator.hub.set_load_level(self._con, level)
        except QLinkError as err:
            raise HomeAssistantError(
                f"Failed to move cover load {self._con} to {level}%: {err}"
            ) from err
        self.coordinator.note_write(self._con, level)
        self.coordinator.apply_level(self._con, level)

    async def async_open_cover(self, **kwargs: Any) -> None:
        await self._async_set_level(100)

    async def async_close_cover(self, **kwargs: Any) -> None:
        await self._async_set_level(0)

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        await self._async_set_level(int(kwargs.get(ATTR_POSITION, 0)))
