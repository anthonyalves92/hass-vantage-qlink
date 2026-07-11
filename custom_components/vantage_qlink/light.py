"""Light platform for Vantage QLink loads (contractor-number addressed)."""

from __future__ import annotations

import math
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_TRANSITION,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util.percentage import (
    percentage_to_ranged_value,
    ranged_value_to_percentage,
)

from .const import CONF_LIGHTS, DEFAULT_FADE, DOMAIN, OPT_DEFAULT_FADE
from .coordinator import QLinkCoordinator
from .hub import QLinkError

BRIGHTNESS_SCALE = (1, 255)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the light platform from the configured contractor numbers."""
    from . import QLinkRuntime, parse_id_list  # avoid circular import

    runtime: QLinkRuntime = hass.data[DOMAIN][entry.entry_id]
    default_fade = entry.options.get(OPT_DEFAULT_FADE, DEFAULT_FADE)
    async_add_entities(
        QLinkLight(runtime.coordinator, con, default_fade)
        for con in parse_id_list(entry.options.get(CONF_LIGHTS))
    )


class QLinkLight(CoordinatorEntity[QLinkCoordinator], LightEntity):
    """A Vantage load exposed as a dimmable light."""

    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_supported_features = LightEntityFeature.TRANSITION

    def __init__(
        self, coordinator: QLinkCoordinator, contractor_number: int, default_fade: float
    ) -> None:
        super().__init__(coordinator)
        self._con = contractor_number
        self._default_fade = default_fade
        # unique_id and device identifiers are unchanged from 0.0.x so
        # existing registry entries (names, areas, automations) reattach.
        self._attr_unique_id = f"vantage_light_{contractor_number}"
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
    def is_on(self) -> bool:
        return self._level > 0

    @property
    def brightness(self) -> int:
        return math.ceil(percentage_to_ranged_value(BRIGHTNESS_SCALE, self._level))

    async def _async_set_level(self, level: int, fade: float) -> None:
        try:
            await self.coordinator.hub.set_load_level(self._con, level, fade)
        except QLinkError as err:
            raise HomeAssistantError(
                f"Failed to set load {self._con} to {level}%: {err}"
            ) from err
        self.coordinator.note_write(self._con, level)
        self.coordinator.apply_level(self._con, level)

    async def async_turn_on(self, **kwargs: Any) -> None:
        level = round(
            ranged_value_to_percentage(
                BRIGHTNESS_SCALE, kwargs.get(ATTR_BRIGHTNESS, 255)
            )
        )
        fade = kwargs.get(ATTR_TRANSITION, self._default_fade)
        await self._async_set_level(max(level, 1), fade)

    async def async_turn_off(self, **kwargs: Any) -> None:
        fade = kwargs.get(ATTR_TRANSITION, self._default_fade)
        await self._async_set_level(0, fade)
