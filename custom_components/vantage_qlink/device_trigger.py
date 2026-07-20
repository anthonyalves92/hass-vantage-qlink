"""Per-button device triggers for Vantage keypad stations.

Turns each programmed keypad button into a device-scoped automation
trigger ("<Button N (Label)> pressed / released"). The button label
(``subtype``) is dynamic, sourced from the imported project at runtime,
so no house-specific data ships in this repo.

Triggers ride the ``vantage_qlink_button`` bus event, which fires
unconditionally on every ``SW`` push (independent of entity readiness),
making them reliable the instant a station device exists.
"""

from __future__ import annotations

import re
from typing import Any

import voluptuous as vol

from homeassistant.components.device_automation import DEVICE_TRIGGER_BASE_SCHEMA
from homeassistant.components.homeassistant.triggers import event as event_trigger
from homeassistant.const import (
    CONF_DEVICE_ID,
    CONF_DOMAIN,
    CONF_PLATFORM,
    CONF_TYPE,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.trigger import TriggerActionType, TriggerInfo
from homeassistant.helpers.typing import ConfigType

from .const import (
    ATTR_BUTTON,
    ATTR_MASTER,
    ATTR_STATION,
    CONF_SUBTYPE,
    DOMAIN,
    EVENT_BUTTON,
    TRIGGER_TYPES,
    station_from_identifiers,
)

TRIGGER_SCHEMA = DEVICE_TRIGGER_BASE_SCHEMA.extend(
    {
        vol.Required(CONF_TYPE): vol.In(TRIGGER_TYPES),
        vol.Required(CONF_SUBTYPE): cv.string,
        vol.Optional(ATTR_MASTER): int,
        vol.Optional(ATTR_STATION): int,
        vol.Optional(ATTR_BUTTON): int,
    }
)

_SUBTYPE_BUTTON_RE = re.compile(r"Button (\d+)")


def _runtime_for_device(hass: HomeAssistant, device: dr.DeviceEntry) -> Any:
    """Return the QLink runtime that owns this device, if any."""
    domain_data = hass.data.get(DOMAIN, {})
    for entry_id in device.config_entries:
        runtime = domain_data.get(entry_id)
        if runtime is not None:
            return runtime
    return None


def _station_buttons(
    hass: HomeAssistant, device: dr.DeviceEntry, master: int, station: int
) -> list[dict[str, Any]]:
    """Buttons for a station: project data first, discovery as fallback.

    Returns a list of ``{"number": int, "label": str | None}``.
    """
    runtime = _runtime_for_device(hass, device)
    if runtime is None:
        return []
    info = runtime.stations.get(f"{master}-{station}", {})
    project_buttons = info.get("buttons")
    if project_buttons:
        out: list[dict[str, Any]] = []
        for btn in project_buttons:
            number = btn.get("number")
            if number is None:
                continue
            out.append({"number": int(number), "label": btn.get("label")})
        return out
    # Fallback: the switch positions discovery found programmed.
    return [
        {"number": int(n), "label": None}
        for n in info.get("programmed_switches", [])
    ]


async def async_get_triggers(
    hass: HomeAssistant, device_id: str
) -> list[dict[str, Any]]:
    """List a pressed/released trigger for every button on the station."""
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        return []
    station = station_from_identifiers(device.identifiers)
    if station is None:
        return []
    master, number = station
    triggers: list[dict[str, Any]] = []
    for btn in _station_buttons(hass, device, master, number):
        label = btn.get("label")
        subtype = f"Button {btn['number']}"
        if label:
            subtype = f"{subtype} ({label})"
        for action in TRIGGER_TYPES:
            triggers.append(
                {
                    CONF_PLATFORM: "device",
                    CONF_DOMAIN: DOMAIN,
                    CONF_DEVICE_ID: device_id,
                    CONF_TYPE: action,
                    CONF_SUBTYPE: subtype,
                    ATTR_MASTER: master,
                    ATTR_STATION: number,
                    ATTR_BUTTON: btn["number"],
                }
            )
    return triggers


async def async_attach_trigger(
    hass: HomeAssistant,
    config: ConfigType,
    action: TriggerActionType,
    trigger_info: TriggerInfo,
) -> CALLBACK_TYPE:
    """Attach by riding the button bus event for this station+button+action."""
    master = config.get(ATTR_MASTER)
    station = config.get(ATTR_STATION)
    button = config.get(ATTR_BUTTON)
    # A hand-written trigger may omit the carried-through address; recover it
    # from the device and the "Button N" subtype.
    if master is None or station is None:
        device = dr.async_get(hass).async_get(config[CONF_DEVICE_ID])
        resolved = station_from_identifiers(device.identifiers) if device else None
        if resolved is not None:
            master, station = resolved
    if button is None:
        match = _SUBTYPE_BUTTON_RE.match(config.get(CONF_SUBTYPE, ""))
        if match:
            button = int(match.group(1))

    event_config = event_trigger.TRIGGER_SCHEMA(
        {
            CONF_PLATFORM: "event",
            event_trigger.CONF_EVENT_TYPE: EVENT_BUTTON,
            event_trigger.CONF_EVENT_DATA: {
                ATTR_MASTER: master,
                ATTR_STATION: station,
                ATTR_BUTTON: button,
                "action": config[CONF_TYPE],
            },
        }
    )
    return await event_trigger.async_attach_trigger(
        hass, event_config, action, trigger_info, platform_type="device"
    )
