"""The Vantage QLink integration.

Full-capacity integration for Vantage Q-series systems behind a QLink
IP Enabler: real-time push updates (VOS/VOL), transitions, keypad
events, system discovery (VQM/VQP/VQS/VGN/VGT), and services exposing
switch functions, LEDs, and controller time functions.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_send

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
    EVENT_ALL_LOADS,
    EVENT_BUTTON,
    EVENT_IR,
    EVENT_LED_CHANGED,
    EVENT_LOAD_CHANGED,
    OPT_COMMAND_TIMEOUT,
    OPT_DEFAULT_FADE,
    OPT_PUSH_LOADS,
    OPT_PUSH_SWITCHES,
    OPT_SCAN_INTERVAL,
    OPT_SEND_GAP_MS,
    SERVICE_DISCOVER,
    SERVICE_EXECUTE_TIME_FUNCTION,
    SERVICE_GET_TIME_FUNCTION,
    SERVICE_PRESS_SWITCH,
    SERVICE_REFRESH,
    SERVICE_SEND_COMMAND,
    SERVICE_SET_LED,
    SERVICE_SET_LOAD_LEVEL,
    SERVICE_SET_PUSH_REPORTING,
    SIGNAL_NEW_STATION,
    STATION_TYPES,
)
from .coordinator import QLinkCoordinator
from .hub import QLinkError, QLinkHub
from .panel import async_remove_panel_if_last, async_setup_panel

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.LIGHT, Platform.COVER, Platform.EVENT]

LED_STATES = {"off": 0, "on": 1, "blink": 2}


def parse_id_list(raw: Any) -> list[int | str]:
    """Parse the comma-separated load-ID option string.

    Two forms, both inherited from 0.0.x:
    - ``1005``   — contractor number (``VGL 1005``)
    - ``2-33-8`` — dash-separated station-bus load address; sent with the
      dashes replaced by spaces (``VGL 2 33 8``), addressing
      master/station/load for LVRS / wall-box dimmer loads.
    """
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        items = [str(i) for i in raw]
    else:
        items = str(raw).split(",")
    out: list[int | str] = []
    for item in items:
        item = item.strip()
        if not item:
            continue
        if item.isdigit():
            out.append(int(item))
        elif all(part.isdigit() for part in item.split("-")) and "-" in item:
            out.append(item)
        else:
            _LOGGER.warning("Ignoring unparseable load id %r", item)
    return out


class QLinkRuntime:
    """Everything the entry owns at runtime."""

    def __init__(
        self, hub: QLinkHub, coordinator: QLinkCoordinator, entry: ConfigEntry
    ) -> None:
        self.hub = hub
        self.coordinator = coordinator
        self.entry = entry
        self.discovery: dict[str, Any] = {}
        self.known_stations: dict[str, dict[str, Any]] = {}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Vantage QLink from a config entry."""
    options = entry.options
    hub = QLinkHub(
        entry.data[CONF_HOST],
        entry.data[CONF_PORT],
        send_gap=options.get(OPT_SEND_GAP_MS, DEFAULT_SEND_GAP_MS) / 1000,
        command_timeout=options.get(OPT_COMMAND_TIMEOUT, DEFAULT_COMMAND_TIMEOUT),
        enable_vos=options.get(OPT_PUSH_SWITCHES, DEFAULT_PUSH_SWITCHES),
        enable_vol=options.get(OPT_PUSH_LOADS, DEFAULT_PUSH_LOADS),
    )

    try:
        await hub.async_connect()
    except QLinkError as err:
        raise ConfigEntryNotReady(
            f"Cannot reach QLink controller (is another client holding the "
            f"single TCP slot?): {err}"
        ) from err

    loads = parse_id_list(options.get(CONF_LIGHTS)) + parse_id_list(
        options.get(CONF_COVERS)
    )
    coordinator = QLinkCoordinator(
        hass,
        hub,
        entry.entry_id,
        loads,
        options.get(OPT_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
    )
    await coordinator.async_load_map()

    runtime = QLinkRuntime(hub, coordinator, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime

    # Sidebar console panel (registered once, shared across entries).
    await async_setup_panel(hass)

    _wire_push_handlers(hass, runtime)
    # Hub callbacks fire from within the event loop (reader task).
    hub.add_connection_callback(lambda up: _on_connection_change(hass, runtime, up))

    await coordinator.async_config_entry_first_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Discover the system topology in the background; feeds the event
    # platform, diagnostics, and the `discover` service.
    entry.async_create_background_task(
        hass, _async_discover(hass, runtime), name=f"{DOMAIN} discovery"
    )

    _register_services(hass)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


def _on_connection_change(
    hass: HomeAssistant, runtime: QLinkRuntime, up: bool
) -> None:
    if up:
        hass.async_create_task(runtime.coordinator.async_request_refresh())
    else:
        runtime.coordinator.async_set_update_error(
            QLinkError("Connection to controller lost")
        )


def _wire_push_handlers(hass: HomeAssistant, runtime: QLinkRuntime) -> None:
    """Route unsolicited controller lines to events + coordinator."""

    coordinator = runtime.coordinator
    entry_id = runtime.entry.entry_id

    def on_push(kind: str, args: list[str]) -> None:
        try:
            _dispatch_push(kind, args)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed handling push %s %s", kind, args)

    def _ints(args: list[str], n: int) -> list[int] | None:
        try:
            return [int(a) for a in args[:n]]
        except ValueError:
            return None

    def _dispatch_push(kind: str, args: list[str]) -> None:
        if kind == "SW":
            vals = _ints(args, 4)
            if not vals or len(vals) < 4:
                return
            master, station, button, state = vals
            payload = {
                "master": master,
                "station": station,
                "button": button,
                "action": "pressed" if state else "released",
            }
            hass.bus.async_fire(EVENT_BUTTON, payload)
            async_dispatcher_send(
                hass, f"{SIGNAL_NEW_STATION}_{entry_id}", payload
            )
        elif kind == "IR":
            vals = _ints(args, 4)
            if not vals or len(vals) < 4:
                return
            master, zone, code, state = vals
            hass.bus.async_fire(
                EVENT_IR,
                {
                    "master": master,
                    "zone": zone,
                    "code": code,
                    "action": "pressed" if state else "released",
                },
            )
        elif kind == "LO":
            vals = _ints(args, 5)
            if not vals or len(vals) < 5:
                return
            master, enclosure, module, load, level = vals
            phys_key = f"{master}-{enclosure}-{module}-{load}"
            coordinator.handle_load_push(phys_key, level)
            hass.bus.async_fire(
                EVENT_LOAD_CHANGED,
                {
                    "scope": "module",
                    "master": master,
                    "enclosure": enclosure,
                    "module": module,
                    "load": load,
                    "level": level,
                    "contractor_number": coordinator.load_map.get(phys_key),
                },
            )
        elif kind == "LS":
            vals = _ints(args, 4)
            if not vals or len(vals) < 4:
                return
            master, station, load, level = vals
            phys_key = f"s{master}-{station}-{load}"
            # Station loads configured in dash form ("2-33-8") are the
            # push address verbatim — map deterministically, no learning.
            direct_id = f"{master}-{station}-{load}"
            if direct_id in coordinator.loads:
                coordinator.apply_level(direct_id, level)
            else:
                coordinator.handle_load_push(phys_key, level)
            hass.bus.async_fire(
                EVENT_LOAD_CHANGED,
                {
                    "scope": "station",
                    "master": master,
                    "station": station,
                    "load": load,
                    "level": level,
                    "contractor_number": coordinator.load_map.get(phys_key),
                },
            )
        elif kind == "LV":
            vals = _ints(args, 3)
            if not vals or len(vals) < 3:
                return
            master, variable, level = vals
            hass.bus.async_fire(
                EVENT_LOAD_CHANGED,
                {
                    "scope": "variable",
                    "master": master,
                    "variable": variable,
                    "level": level,
                },
            )
        elif kind in ("AON", "AOFF"):
            hass.bus.async_fire(
                EVENT_ALL_LOADS,
                {"action": "all_on" if kind == "AON" else "all_off"},
            )
            hass.async_create_task(coordinator.async_request_refresh())
        elif kind in ("LE", "LC"):
            hass.bus.async_fire(EVENT_LED_CHANGED, {"kind": kind, "args": args})

    runtime.hub.add_push_callback(on_push)


async def _async_discover(hass: HomeAssistant, runtime: QLinkRuntime) -> None:
    """Enumerate masters, modules, stations, and names from the controller."""
    hub = runtime.hub
    discovery: dict[str, Any] = {"masters": [], "modules": [], "stations": []}
    try:
        try:
            discovery["port_info"] = await hub.probe()
        except QLinkError:
            discovery["port_info"] = None

        masters = await hub.query_masters()
        discovery["masters"] = masters

        for master in masters:
            try:
                discovery["modules"].extend(await hub.query_modules(master))
            except QLinkError as err:
                _LOGGER.debug("VQP failed for master %s: %s", master, err)
            try:
                stations = await hub.query_stations(master)
            except QLinkError as err:
                _LOGGER.debug("VQS failed for master %s: %s", master, err)
                continue

            for st in stations:
                key = f"{st['master']}-{st['station']}"
                st["type_name"] = STATION_TYPES.get(st["type"], f"type {st['type']}")
                try:
                    st["name"] = await hub.get_name(st["master"], st["station"], 255)
                except QLinkError:
                    st["name"] = ""
                # Which of the 10 switch positions are programmed?
                try:
                    states = await hub.get_station_switches(
                        st["master"], st["station"]
                    )
                    st["programmed_switches"] = [
                        i + 1 for i, s in enumerate(states) if s in (0, 1)
                    ]
                except QLinkError:
                    st["programmed_switches"] = []
                discovery["stations"].append(st)
                runtime.known_stations[key] = st
                async_dispatcher_send(
                    hass,
                    f"{SIGNAL_NEW_STATION}_{runtime.entry.entry_id}",
                    {"master": st["master"], "station": st["station"]},
                )

        runtime.discovery = discovery
        _LOGGER.info(
            "QLink discovery: %d master(s), %d module(s), %d station(s)",
            len(discovery["masters"]),
            len(discovery["modules"]),
            len(discovery["stations"]),
        )
    except QLinkError as err:
        _LOGGER.warning("QLink discovery incomplete: %s", err)
        runtime.discovery = discovery


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        runtime: QLinkRuntime | None = hass.data.get(DOMAIN, {}).pop(
            entry.entry_id, None
        )
        if runtime:
            await runtime.coordinator.async_shutdown()
            await runtime.hub.async_disconnect()
        async_remove_panel_if_last(hass)
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Entry removed permanently: turn push reporting back off.

    Leaves the controller's serial port the way a plain request/response
    client (e.g. the original 0.0.x integration) expects to find it.
    """
    hub = QLinkHub(entry.data[CONF_HOST], entry.data[CONF_PORT])
    try:
        await hub.async_connect()
        await hub.async_set_push_reporting(False, False)
    except QLinkError:
        _LOGGER.debug("Could not disable push reporting during removal")
    finally:
        await hub.async_disconnect()


# --------------------------------------------------------------- services


def _get_runtime(hass: HomeAssistant, call: ServiceCall) -> QLinkRuntime:
    domain_data = {
        k: v
        for k, v in hass.data.get(DOMAIN, {}).items()
        if not k.startswith("_")
    }
    if not domain_data:
        raise HomeAssistantError("No Vantage QLink entry is set up")
    entry_id = call.data.get("entry_id")
    if entry_id:
        if entry_id not in domain_data:
            raise HomeAssistantError(f"Unknown entry_id {entry_id}")
        return domain_data[entry_id]
    return next(iter(domain_data.values()))


def _register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_SEND_COMMAND):
        return

    async def send_command(call: ServiceCall) -> ServiceResponse:
        runtime = _get_runtime(hass, call)
        try:
            lines = await runtime.hub.raw(
                call.data["command"], timeout=call.data.get("timeout")
            )
        except QLinkError as err:
            raise HomeAssistantError(f"Command failed: {err}") from err
        return {"response": lines}

    async def set_load_level(call: ServiceCall) -> None:
        runtime = _get_runtime(hass, call)
        con = call.data["contractor_number"]
        level = call.data["level"]
        fade = call.data.get("fade", 0.0)
        try:
            await runtime.hub.set_load_level(con, level, fade)
        except QLinkError as err:
            raise HomeAssistantError(f"VLO failed: {err}") from err
        runtime.coordinator.note_write(con, level)
        runtime.coordinator.apply_level(con, level)

    async def press_switch(call: ServiceCall) -> None:
        runtime = _get_runtime(hass, call)
        master = call.data["master"]
        station = call.data["station"]
        switch = call.data["switch"]
        action = call.data.get("action", "momentary")
        try:
            if action == "momentary":
                await runtime.hub.execute_switch(master, station, switch, 1)
                await asyncio.sleep(call.data.get("hold_time", 0.3))
                await runtime.hub.execute_switch(master, station, switch, 0)
            else:
                await runtime.hub.execute_switch(
                    master, station, switch, 1 if action == "press" else 0
                )
        except QLinkError as err:
            raise HomeAssistantError(f"VSW failed: {err}") from err

    async def set_led(call: ServiceCall) -> None:
        runtime = _get_runtime(hass, call)
        try:
            await runtime.hub.set_led(
                call.data["master"],
                call.data["station"],
                call.data["led"],
                LED_STATES[call.data["state"]],
            )
        except QLinkError as err:
            raise HomeAssistantError(f"VLD failed: {err}") from err

    async def execute_time_function(call: ServiceCall) -> None:
        runtime = _get_runtime(hass, call)
        try:
            await runtime.hub.execute_time_function(
                call.data["master"],
                call.data["function"],
                1 if call.data.get("state", "on") == "on" else 0,
            )
        except QLinkError as err:
            raise HomeAssistantError(f"VET failed: {err}") from err

    async def get_time_function(call: ServiceCall) -> ServiceResponse:
        runtime = _get_runtime(hass, call)
        try:
            raw = await runtime.hub.get_time_function(
                call.data["master"], call.data["function"]
            )
        except QLinkError as err:
            raise HomeAssistantError(f"VQT failed: {err}") from err
        return {"raw": raw}

    async def discover(call: ServiceCall) -> ServiceResponse:
        runtime = _get_runtime(hass, call)
        return {
            "discovery": runtime.discovery,
            "learned_load_map": runtime.coordinator.load_map,
        }

    async def refresh(call: ServiceCall) -> None:
        runtime = _get_runtime(hass, call)
        await runtime.coordinator.async_request_refresh()

    async def set_push_reporting(call: ServiceCall) -> None:
        runtime = _get_runtime(hass, call)
        try:
            await runtime.hub.async_set_push_reporting(
                call.data.get("switches", True), call.data.get("loads", True)
            )
        except QLinkError as err:
            raise HomeAssistantError(f"VOS/VOL failed: {err}") from err

    entry_opt = {vol.Optional("entry_id"): cv.string}

    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND_COMMAND,
        send_command,
        schema=vol.Schema(
            {
                vol.Required("command"): cv.string,
                vol.Optional("timeout"): vol.Coerce(float),
                **entry_opt,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_LOAD_LEVEL,
        set_load_level,
        schema=vol.Schema(
            {
                vol.Required("contractor_number"): vol.Coerce(int),
                vol.Required("level"): vol.All(
                    vol.Coerce(int), vol.Range(min=0, max=100)
                ),
                vol.Optional("fade"): vol.All(
                    vol.Coerce(float), vol.Range(min=0, max=6553.5)
                ),
                **entry_opt,
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_PRESS_SWITCH,
        press_switch,
        schema=vol.Schema(
            {
                vol.Required("master"): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=15)
                ),
                vol.Required("station"): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=124)
                ),
                vol.Required("switch"): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=10)
                ),
                vol.Optional("action", default="momentary"): vol.In(
                    ["momentary", "press", "release"]
                ),
                vol.Optional("hold_time", default=0.3): vol.All(
                    vol.Coerce(float), vol.Range(min=0.05, max=10)
                ),
                **entry_opt,
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_LED,
        set_led,
        schema=vol.Schema(
            {
                vol.Required("master"): vol.Coerce(int),
                vol.Required("station"): vol.Coerce(int),
                vol.Required("led"): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=10)
                ),
                vol.Required("state"): vol.In(list(LED_STATES)),
                **entry_opt,
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_EXECUTE_TIME_FUNCTION,
        execute_time_function,
        schema=vol.Schema(
            {
                vol.Required("master"): vol.Coerce(int),
                vol.Required("function"): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=255)
                ),
                vol.Optional("state", default="on"): vol.In(["on", "off"]),
                **entry_opt,
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_TIME_FUNCTION,
        get_time_function,
        schema=vol.Schema(
            {
                vol.Required("master"): vol.Coerce(int),
                vol.Required("function"): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=255)
                ),
                **entry_opt,
            }
        ),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_DISCOVER,
        discover,
        schema=vol.Schema({**entry_opt}),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_REFRESH, refresh, schema=vol.Schema({**entry_opt})
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_PUSH_REPORTING,
        set_push_reporting,
        schema=vol.Schema(
            {
                vol.Optional("switches", default=True): cv.boolean,
                vol.Optional("loads", default=True): cv.boolean,
                **entry_opt,
            }
        ),
    )
