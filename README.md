# Vantage QLink — full-capacity Home Assistant integration

Home Assistant integration for **Vantage Q-series** lighting systems behind a
**QLink IP Enabler** (serial-over-TCP, typically port 10001). This is a
substantially extended fork of
[bassrock/hass-vantage-qlink](https://github.com/bassrock/hass-vantage-qlink)
that upgrades it from poll-only lights to a push-driven integration covering
the whole QLink protocol surface.

A companion Node.js debug bridge / web console for the same protocol lives at
[anthonyalves92/vantage-qlink-api](https://github.com/anthonyalves92/vantage-qlink-api).

## What it does

- **Lights & covers** by contractor number (`VGL`/`VLO`), with **transition
  support** (`light.turn_on` `transition:` maps to the QLink fade parameter,
  0–6553.5 s).
- **Real-time push updates** — enables the controller's persistent reporting:
  - `VOL` load changes (`LO`/`LS`/`LV` lines) update entities instantly;
  - `VOS` keypad presses (`SW` lines) fire `vantage_qlink_button` events and
    per-station **event entities** — every physical keypad button becomes an
    automation trigger in the UI;
  - IR receivers fire `vantage_qlink_ir` events.
- **Single-connection hub** with command queueing, send-gap pacing, `#`
  detailed-response matching, and automatic reconnect. (The IP Enabler
  accepts exactly one TCP client — this integration owns it.)
- **Discovery** — enumerates masters (`VQM`), modules (`VQP`), stations
  (`VQS`), pulls names stored in the controller (`VGN`), and probes which
  keypad buttons are programmed (`VGT`). See it under the entry's
  **diagnostics** or via the `vantage_qlink.discover` service.
- **Self-learning load map** — `VOL` pushes identify loads by physical
  address (master/enclosure/module/load) while entities use contractor
  numbers; the integration learns the mapping automatically by correlating
  pushes with HA-initiated writes and poll-sweep diffs, and persists it.
  Mapped loads get instant push updates with no polling latency.
- **Services** exposing the rest of the protocol:

  | Service | QLink | Purpose |
  |---|---|---|
  | `vantage_qlink.press_switch` | `VSW` | Execute any keypad switch function — trigger Vantage scenes/macros |
  | `vantage_qlink.set_load_level` | `VLO` | Level + fade by contractor number |
  | `vantage_qlink.set_led` | `VLD` | Keypad button LED off/on/blink |
  | `vantage_qlink.execute_time_function` | `VET` | Run a controller scheduled function |
  | `vantage_qlink.get_time_function` | `VQT` | Read a scheduled function's parameters (returns response) |
  | `vantage_qlink.send_command` | any | Raw command escape hatch (returns response) |
  | `vantage_qlink.discover` | `VQM/VQP/VQS/VGN/VGT` | Topology + learned map (returns response) |
  | `vantage_qlink.refresh` | `VGL` sweep | Force an immediate poll |
  | `vantage_qlink.set_push_reporting` | `VOS/VOL` | Toggle controller push reporting |

- **Options** (Settings → Devices & Services → Vantage QLink → Configure):
  load lists, sweep interval, send gap, command timeout, default fade, and
  push toggles.

## Compatibility with 0.0.x

Drop-in: same domain, same config entry data, same options keys, same
`vantage_light_<n>` / `vantage_cover_<n>` unique IDs and device identifiers.
Existing entities, names, areas, automations, and dashboards reattach
unchanged after updating.

> **Note:** push reporting (`VOS`/`VOL`) is *persistent on the controller's
> serial port*. If you ever downgrade to a plain request/response client
> (including the original 0.0.x integration), first call
> `vantage_qlink.set_push_reporting` with `switches: false, loads: false` —
> otherwise unsolicited push lines will confuse it. Removing the config
> entry does this automatically.

## Install

Via HACS as a custom repository (`anthonyalves92/hass-vantage-qlink`,
category *Integration*), then restart Home Assistant. Configure via
Settings → Devices & Services → Add Integration → Vantage QLink.

## Events

```yaml
# Any keypad press, anywhere in the house:
trigger:
  - platform: event
    event_type: vantage_qlink_button
    event_data:
      master: 1
      station: 12
      button: 3
      action: pressed
```

`vantage_qlink_load_changed` fires on every `VOL` push (with
`contractor_number` when the mapping is known), `vantage_qlink_ir` on IR
codes, `vantage_qlink_led_changed` on LED reports (if `VOD` is enabled
out-of-band), and `vantage_qlink_all_loads` on system-wide ALL ON / ALL OFF.

## License

GPL-3.0, same as the upstream project it extends.
