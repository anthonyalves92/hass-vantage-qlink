"""Device action: set a keypad button LED on a Vantage station.

Puts LED control (VLD off/on/blink) directly on the station device in the
automation UI, without creating per-LED entities (LED read-back is
unreliable, and one entity per LED would be ~10 per keypad). The existing
``vantage_qlink.set_led`` service remains for power users.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.const import CONF_DEVICE_ID, CONF_DOMAIN, CONF_TYPE
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.typing import ConfigType, TemplateVarsType

from .const import (
    ACTION_SET_LED,
    ATTR_LED,
    ATTR_STATE,
    DOMAIN,
    LED_STATES,
    station_from_identifiers,
)
from .hub import QLinkError

ACTION_SCHEMA = cv.DEVICE_ACTION_BASE_SCHEMA.extend(
    {
        vol.Required(CONF_TYPE): vol.In([ACTION_SET_LED]),
        vol.Required(ATTR_LED): vol.All(vol.Coerce(int), vol.Range(min=1, max=10)),
        vol.Required(ATTR_STATE): vol.In(list(LED_STATES)),
    }
)


async def async_get_actions(
    hass: HomeAssistant, device_id: str
) -> list[dict[str, Any]]:
    """Offer the ``set_led`` action on every keypad station device."""
    device = dr.async_get(hass).async_get(device_id)
    if device is None or station_from_identifiers(device.identifiers) is None:
        return []
    return [
        {
            CONF_DEVICE_ID: device_id,
            CONF_DOMAIN: DOMAIN,
            CONF_TYPE: ACTION_SET_LED,
        }
    ]


async def async_call_action_from_config(
    hass: HomeAssistant,
    config: ConfigType,
    variables: TemplateVarsType,
    context: Context | None,
) -> None:
    """Execute a ``set_led`` device action."""
    device = dr.async_get(hass).async_get(config[CONF_DEVICE_ID])
    station = (
        station_from_identifiers(device.identifiers) if device is not None else None
    )
    if station is None:
        raise HomeAssistantError("Device is not a Vantage keypad station")
    master, number = station

    domain_data = hass.data.get(DOMAIN, {})
    runtime = next(
        (domain_data[e] for e in device.config_entries if e in domain_data),
        None,
    )
    if runtime is None:
        raise HomeAssistantError("Vantage QLink entry for this device is not loaded")

    try:
        await runtime.hub.set_led(
            master, number, config[ATTR_LED], LED_STATES[config[ATTR_STATE]]
        )
    except QLinkError as err:
        raise HomeAssistantError(f"VLD failed: {err}") from err
