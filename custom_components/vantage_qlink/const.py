"""Constants for the Vantage QLink integration."""

DOMAIN = "vantage_qlink"

# Config entry data / options keys.
# CONF_LIGHTS / CONF_COVERS are comma-separated contractor-number strings —
# unchanged from 0.0.x so existing entries keep working without migration.
CONF_LIGHTS = "vantage_lights"
CONF_COVERS = "vantage_covers"

OPT_SCAN_INTERVAL = "scan_interval"
OPT_SEND_GAP_MS = "send_gap_ms"
OPT_COMMAND_TIMEOUT = "command_timeout"
OPT_DEFAULT_FADE = "default_fade"
OPT_PUSH_SWITCHES = "push_switches"  # VOS
OPT_PUSH_LOADS = "push_loads"  # VOL

DEFAULT_SCAN_INTERVAL = 60  # seconds between full VGL sweeps
DEFAULT_SEND_GAP_MS = 120  # pacing between on-wire commands
DEFAULT_COMMAND_TIMEOUT = 4.0
# Matches the ramp Vantage keypad scenes use, per side-by-side comparison
# against a keypad press on real hardware. Override per-call with
# light.turn_on's `transition`, or per-install in the options flow.
DEFAULT_FADE = 3.0
DEFAULT_PUSH_SWITCHES = True
DEFAULT_PUSH_LOADS = True

# Events fired on the Home Assistant bus.
EVENT_BUTTON = f"{DOMAIN}_button"
EVENT_IR = f"{DOMAIN}_ir"
EVENT_LOAD_CHANGED = f"{DOMAIN}_load_changed"
EVENT_LED_CHANGED = f"{DOMAIN}_led_changed"
EVENT_ALL_LOADS = f"{DOMAIN}_all_loads"

# Dispatcher signals (suffixed with entry_id at runtime).
SIGNAL_NEW_STATION = f"{DOMAIN}_new_station"

# Services.
SERVICE_SEND_COMMAND = "send_command"
SERVICE_SET_LOAD_LEVEL = "set_load_level"
SERVICE_PRESS_SWITCH = "press_switch"
SERVICE_SET_LED = "set_led"
SERVICE_EXECUTE_TIME_FUNCTION = "execute_time_function"
SERVICE_GET_TIME_FUNCTION = "get_time_function"
SERVICE_DISCOVER = "discover"
SERVICE_REFRESH = "refresh"
SERVICE_SET_PUSH_REPORTING = "set_push_reporting"

# Station types per the QLink protocol reference (VQS).
STATION_TYPES = {
    0: "Keypad",
    1: "Contact Input",
    2: "LV Relay",
    3: "Infrared Emitter",
    4: "0-10V",
    5: "Dimming",
    6: "LCD",
}

# Module types per the QLink protocol reference (VQP).
MODULE_TYPES = {
    2: "AR8008-120 / AR18008-277",
    3: "CAR160A",
    5: "SD4008-120 / SD9008-277",
    6: "ED4008-120",
}
