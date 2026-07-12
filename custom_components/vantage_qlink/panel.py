"""Sidebar console panel for Vantage QLink.

Registers a custom frontend panel (the equivalent of the standalone
vantage-qlink-api web console) that reuses the integration's single
controller connection instead of competing for the IP Enabler's one
TCP slot. Data flows over a websocket command; raw commands go through
the existing send_command service.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import voluptuous as vol

from homeassistant.components import frontend, websocket_api
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN

PANEL_URL_PATH = "vantage-qlink"
PANEL_STATIC_PATH = "/vantage_qlink_files"
PANEL_FLAG = "_panel_registered"


async def async_setup_panel(hass: HomeAssistant) -> None:
    """Register the sidebar panel and its websocket backend (once)."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get(PANEL_FLAG):
        return
    domain_data[PANEL_FLAG] = True

    await hass.http.async_register_static_paths(
        [
            StaticPathConfig(
                PANEL_STATIC_PATH,
                str(Path(__file__).parent / "www"),
                cache_headers=False,
            )
        ]
    )

    websocket_api.async_register_command(hass, ws_panel_data)

    frontend.async_register_built_in_panel(
        hass,
        component_name="custom",
        sidebar_title="Vantage QLink",
        sidebar_icon="mdi:console-line",
        frontend_url_path=PANEL_URL_PATH,
        require_admin=True,
        config={
            "_panel_custom": {
                "name": "vantage-qlink-panel",
                "embed_iframe": False,
                "trust_external": False,
                "module_url": f"{PANEL_STATIC_PATH}/vantage-qlink-panel.js",
            }
        },
    )


@callback
def async_remove_panel_if_last(hass: HomeAssistant) -> None:
    """Remove the sidebar panel when the last config entry unloads."""
    domain_data = hass.data.get(DOMAIN, {})
    entries_left = [k for k in domain_data if not k.startswith("_")]
    if entries_left:
        return
    if domain_data.pop(PANEL_FLAG, None):
        frontend.async_remove_panel(hass, PANEL_URL_PATH)


@websocket_api.websocket_command({vol.Required("type"): "vantage_qlink/panel_data"})
@websocket_api.async_response
async def ws_panel_data(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return console state: connection, traffic, discovery, load map."""
    domain_data = hass.data.get(DOMAIN, {})
    runtime = next(
        (v for k, v in domain_data.items() if not k.startswith("_")), None
    )
    if runtime is None:
        connection.send_result(msg["id"], {"loaded": False})
        return

    hub = runtime.hub
    coordinator = runtime.coordinator
    connection.send_result(
        msg["id"],
        {
            "loaded": True,
            "connection": {
                "host": hub.host,
                "port": hub.port,
                "connected": hub.connected,
                "send_gap_ms": round(hub.send_gap * 1000),
                "push_switches": hub.enable_vos,
                "push_loads": hub.enable_vol,
            },
            "traffic": list(hub.recent_lines),
            "discovery": runtime.discovery,
            "learned_map": coordinator.load_map,
            "load_count": len(coordinator.loads),
            "levels": {str(k): v for k, v in (coordinator.data or {}).items()},
        },
    )
