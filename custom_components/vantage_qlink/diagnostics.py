"""Diagnostics for the Vantage QLink integration."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if runtime is None:
        return {"error": "runtime not loaded"}

    coordinator = runtime.coordinator
    hub = runtime.hub
    return {
        "connection": {
            "host": hub.host,
            "port": hub.port,
            "connected": hub.connected,
            "send_gap": hub.send_gap,
            "push_switches": hub.enable_vos,
            "push_loads": hub.enable_vol,
        },
        "loads": {
            "configured": coordinator.loads,
            "levels": coordinator.data,
            "learned_physical_map": coordinator.load_map,
        },
        "discovery": runtime.discovery,
        "recent_traffic": list(hub.recent_lines),
    }
