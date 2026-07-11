"""Event entities for Vantage keypad stations.

One event entity per discovered station. It fires on VOS push lines
(``SW <master> <station> <switch> <state>``) with the button number in
the event data, making keypad presses first-class automation triggers
in the UI. Stations that appear at runtime (a press from a station
discovery hadn't listed yet) get an entity on the fly.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_NEW_STATION

EVENT_TYPES = ["pressed", "released"]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create event entities for known stations; add new ones on the fly."""
    from . import QLinkRuntime  # avoid circular import

    runtime: QLinkRuntime = hass.data[DOMAIN][entry.entry_id]
    entities: dict[str, QLinkStationEvent] = {}

    def _ensure_station(master: int, station: int) -> QLinkStationEvent | None:
        key = f"{master}-{station}"
        if key in entities:
            return entities[key]
        info = runtime.known_stations.get(key, {})
        entity = QLinkStationEvent(entry.entry_id, master, station, info)
        entities[key] = entity
        async_add_entities([entity])
        return entity

    @callback
    def _on_signal(payload: dict[str, Any]) -> None:
        master = payload.get("master")
        station = payload.get("station")
        if master is None or station is None:
            return
        entity = _ensure_station(int(master), int(station))
        if entity is not None and "button" in payload:
            entity.handle_press(payload)

    entry.async_on_unload(
        async_dispatcher_connect(
            hass, f"{SIGNAL_NEW_STATION}_{entry.entry_id}", _on_signal
        )
    )

    # Stations already discovered before this platform loaded.
    for info in list(runtime.known_stations.values()):
        _ensure_station(info["master"], info["station"])


class QLinkStationEvent(EventEntity):
    """Keypad press/release events for one Vantage station."""

    _attr_event_types = EVENT_TYPES
    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "station_buttons"

    def __init__(
        self, entry_id: str, master: int, station: int, info: dict[str, Any]
    ) -> None:
        self._master = master
        self._station = station
        self._attr_unique_id = f"vantage_station_{master}_{station}"

        raw_name = (info.get("name") or "").split("|")[0].strip()
        station_label = raw_name or f"Station {master}-{station}"
        model = info.get("type_name") or "Keypad Station"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"station_{master}_{station}")},
            name=f"Vantage {station_label}",
            manufacturer="Vantage",
            model=model,
            serial_number=str(info.get("serial", "")) or None,
        )
        self._attr_extra_state_attributes = {
            "master": master,
            "station": station,
            "programmed_switches": info.get("programmed_switches", []),
        }

    @callback
    def handle_press(self, payload: dict[str, Any]) -> None:
        """Record a press/release from the VOS push stream."""
        if self.hass is None or self.entity_id is None:
            return  # entity not fully added yet; the bus event still fired
        self._trigger_event(
            payload.get("action", "pressed"), {"button": payload.get("button")}
        )
        self.async_write_ha_state()
