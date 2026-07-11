"""Config flow for the Vantage QLink integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError

from .const import (
    CONF_COVERS,
    CONF_LIGHTS,
    DEFAULT_COMMAND_TIMEOUT,
    DEFAULT_FADE,
    DEFAULT_PUSH_LOADS,
    DEFAULT_PUSH_SWITCHES,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SEND_GAP_MS,
    DOMAIN,
    OPT_COMMAND_TIMEOUT,
    OPT_DEFAULT_FADE,
    OPT_PUSH_LOADS,
    OPT_PUSH_SWITCHES,
    OPT_SCAN_INTERVAL,
    OPT_SEND_GAP_MS,
)
from .hub import QLinkError, QLinkHub

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=10001): int,
        vol.Optional(CONF_LIGHTS, default=""): str,
        vol.Optional(CONF_COVERS, default=""): str,
    }
)


async def _validate_connection(host: str, port: int) -> None:
    """Open a probe connection; the IP Enabler allows one client only."""
    hub = QLinkHub(host, port, enable_vos=False, enable_vol=False)
    try:
        await hub.async_connect()
        await hub.probe()
    finally:
        await hub.async_disconnect()


class VantageQLinkConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the initial setup flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await _validate_connection(
                    user_input[CONF_HOST], user_input[CONF_PORT]
                )
            except QLinkError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected exception validating connection")
                errors["base"] = "unknown"

            if not errors:
                return self.async_create_entry(
                    title=f"Vantage QLink {user_input[CONF_HOST]}",
                    data={
                        CONF_HOST: user_input[CONF_HOST],
                        CONF_PORT: user_input[CONF_PORT],
                    },
                    options={
                        CONF_LIGHTS: user_input.get(CONF_LIGHTS, ""),
                        CONF_COVERS: user_input.get(CONF_COVERS, ""),
                    },
                )

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> OptionsFlowHandler:
        return OptionsFlowHandler()


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Options: load lists plus tuning and push-reporting toggles."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_LIGHTS, default=options.get(CONF_LIGHTS, "")
                ): str,
                vol.Optional(
                    CONF_COVERS, default=options.get(CONF_COVERS, "")
                ): str,
                vol.Optional(
                    OPT_SCAN_INTERVAL,
                    default=options.get(OPT_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                ): vol.All(vol.Coerce(int), vol.Range(min=15, max=3600)),
                vol.Optional(
                    OPT_SEND_GAP_MS,
                    default=options.get(OPT_SEND_GAP_MS, DEFAULT_SEND_GAP_MS),
                ): vol.All(vol.Coerce(int), vol.Range(min=30, max=1000)),
                vol.Optional(
                    OPT_COMMAND_TIMEOUT,
                    default=options.get(
                        OPT_COMMAND_TIMEOUT, DEFAULT_COMMAND_TIMEOUT
                    ),
                ): vol.All(vol.Coerce(float), vol.Range(min=1, max=30)),
                vol.Optional(
                    OPT_DEFAULT_FADE,
                    default=options.get(OPT_DEFAULT_FADE, DEFAULT_FADE),
                ): vol.All(vol.Coerce(float), vol.Range(min=0, max=60)),
                vol.Optional(
                    OPT_PUSH_SWITCHES,
                    default=options.get(OPT_PUSH_SWITCHES, DEFAULT_PUSH_SWITCHES),
                ): bool,
                vol.Optional(
                    OPT_PUSH_LOADS,
                    default=options.get(OPT_PUSH_LOADS, DEFAULT_PUSH_LOADS),
                ): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""
