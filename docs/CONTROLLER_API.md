# Controller API

The Raspberry Pi controller is the source of truth. Home Assistant and WebUI are clients only.

All machine endpoints require `Authorization: Bearer <API key>`. Keys are made in the WebUI under
**Opsætning → Adgang for programmer**, look like `dth_` + 43 characters, and are stored only as a SHA-256 hash.
A key is never accepted in the URL. The token from 1.8.x and earlier (`controller-token.json` or
`DANTHERM_CONTROLLER_TOKEN`) was moved in as the key *Home Assistant* with the scope `control` and keeps working.

| Scope | WebUI name | Endpoints |
| --- | --- | --- |
| `read` | Kun læse | `GET /api/controller/state`, `GET /api/controller/rooms` |
| `control` | Læse og styre | `read` + `POST /api/controller/heartbeat`, `/inputs`, `/signals`, `/command`, `/ha-catalog` |
| `admin` | Administrator | `control` + `POST /api/controller/config` (settings, like a technician in the WebUI) |

No key can manage users, keys, the Kontrolrum sign-in or the Pi itself.

Answers: `401` unknown, deleted or expired key; `403` the key lacks the scope, or it is limited to the home
network and the call came from outside or through a proxy/tunnel (`X-Forwarded-For`, `Forwarded`,
`CF-Connecting-IP` …); `429` with `Retry-After` when a key makes more than 120 reads, 240 heartbeat/inputs/signals
or 60 command/config calls a minute, or an address sent 10 wrong keys within 5 minutes (a valid key is never held
back by that). Changes made with a key show the key's name in the change log (`apikey:<name>`; the moved key keeps
`home_assistant`).

## GET /api/controller/state

Returns persistent controller configuration, desired/effective state, actual HCH/HAC1 state, master arbitration and smart-room diagnostics.

## POST /api/controller/command

Accepts the same persistent configuration fields as the WebUI, except `enabled` is forbidden because the Pi controller cannot be user-disabled.

Examples:

```json
{"mode":"manual","manual_level":4}
```

```json
{"mode":"smart_auto"}
```

```json
{"afterheat_setpoint":22}
```

The HAC1 afterheat setpoint writes only the verified FC16 block 185..189;
register 186 carries the requested setpoint in degrees Celsius multiplied by
256, with the other words kept to their captured values. No standalone
single-register write has been verified.
The live temperature block 180..184 is refreshed independently every four
seconds while the Pi is the permitted bus master, preserving the outdoor
temperature used by HAC1's afterheat lockout. `t3_setpoint` and `t5_setpoint`
are currently persisted locally only; no verified Modbus write mapping exists
for either setting.

Live T2, T2AH, HAC1 frost, and HRC2 T5 readings are hidden from state and
history when no valid sample has arrived for 45 seconds. Missing samples are
stored as gaps, not as repeated copies of the last value. Historical rows
without freshness markers from before this change are treated as unverifiable
and their affected temperature values are hidden.

The controller-aware gateway enables read-only active polling by default;
`serial.active_reads_enabled: false` remains an opt-out. Active polls run only
when Pi owns the bus or after the configured quiet-time takeover probe. Valid
HCP4 FC03/FC04 read requests and FC06/FC16 writes mark HCP4 active, pausing
Pi polls until the bus has been quiet for the master release timeout (10
seconds by default). The passive gateway entry point keeps active polling
disabled unless explicitly enabled.

```json
{"fireplace":true}
```

## POST /api/controller/inputs

Leased Home Assistant room measurements. They are never written directly to Modbus.

```json
{
  "source":"home_assistant",
  "valid_for_s":180,
  "rooms":{
    "bathroom":{"temperature":22.8,"humidity":68.4,"enabled":true,"control":true,"priority":"high"},
    "bedroom":{"co2":1180,"enabled":true,"control":true,"priority":"auto"},
    "kitchen":{"co2":920,"enabled":true,"control":false,"priority":"low"}
  }
}
```

Pi derives levels 1–4 (or 1–6 with six-step control, `fan_step_count`) from worst-room CO2/RH and a 10-minute RH rise trigger. Priority changes how early a room reacts, but a severe normal-priority measurement still beats a mild high-priority measurement. Disabled and monitor-only rooms never control ventilation. The HCH5/HAC1 local CO2/RH sensors remain part of the decision. Stale leased input automatically falls back to Local Auto with the configured downshift/boost protection.

At most 32 rooms are accepted. Invalid metadata or out-of-range measurements return HTTP 400 instead of being silently used. Bypass is status-only in this beta; non-`auto` commands are rejected because the verified hardware sequence is not documented.

## POST /api/controller/signals

Leased external switches, machine token required. Today only `fireplace`: while `true` and *Automatic fireplace mode* is enabled in the WebUI, the Pi holds the unit's fireplace mode (bypass closed) and keeps it for the configured afterrun after the signal ends. The lease (`valid_for_s`, 30–900 s, default 300) must be renewed; an expired lease counts as `false`.

```json
{"fireplace":true,"valid_for_s":300}
```

Rooms sent to `/api/controller/inputs` can also serve as measurement sources chosen in the WebUI: a stove temperature for automatic fireplace mode, outdoor humidity for absolute-humidity control, and a room temperature for afterheat. Rooms selected as stove or outdoor sources never take part in air-quality decisions; send them with `"control": false`, or give them the type `source`, `outdoor` or `extract` on the page Rum.

## Rooms: POST /api/controller/ha-catalog and GET /api/controller/rooms

Rooms are set up in the WebUI on the page **Opsætning → Rum** and stored in the controller config as `room_setup`
(a list of rooms: `id`, `name`, `area_id`, `type`, `priority`, `control`, `enabled`, `unit_sensor`, `sensors`
`{temperature, humidity, co2, pm25}` and, for bathrooms, optional `rh_setpoint`, `rh_hysteresis`, `max_level`; for rooms that control, an optional `co2_limit` in ppm, 600–2000, that replaces the house `co2_setpoint` for that room only — leave it out for the house value).
Types: `normal`, `bedroom`, `bathroom`, `kitchen` (indoor rooms in the control), `source`, `outdoor`, `extract`
(measuring only, usable as sources) and `excluded` (not ventilated, ignored everywhere). At most one room has
`unit_sensor` – the room the unit's own sensor (T5 and RH) sits in; the unit fills in what HA does not measure there.
Technicians and administrators save it with `POST /api/controller/config` `{"room_setup": [...]}`.

Home Assistant sends its areas and their sensors (start-up and every 10 minutes):

```json
{"areas": [{"area_id": "stue", "area_name": "Stue", "sensors": [
  {"entity_id": "sensor.stue_temperature", "name": "Stue temperatur", "kind": "temperature", "unit": "°C"}]}],
 "legacy_rooms": {"Bad": {"room_type": "bathroom", "entities": {"humidity": "sensor.bad_rh"}}}}
```

`legacy_rooms` (the rooms from the integration's old options flow) become the room setup once, when the Pi has
none and never had one. The answer, like `GET /api/controller/rooms`, is the room setup, the catalogue and its age.
HA then sends the values of the chosen sensors to `/api/controller/inputs` under the room names; the room setup on
the Pi decides type, priority and control, and drops excluded rooms.

## Master rule

The user-facing temporary **OFF** control stops ventilation for a selected duration and then restores the prior operating state; it does not disable RS485 master arbitration. Pi becomes master automatically whenever the RS485 bus is healthy and HCP4 is absent. Any detected HCP4 FC06/FC16 activity immediately pauses Pi writes. During `unknown` arbitration state writes are blocked.

## Fan steps

`fan_step_count` is `4` (default, Dantherm HCP4/HRC 2) or `6` (free table). Posting `{"fan_step_count": 6}` switches at once and moves every stored step choice to the step with the nearest fan gears; the other model's values are kept.

With four steps the steps come from `fan_settings`: `{"supply": 46..91, "extract": 46..91, "offset": 10..30, "max_supply": supply..100, "max_extract": extract..100}` (partial objects are merged). Steps 1 and 2 are step 3 minus two and one offsets (at least gear 1); step 4 is the maximum. A `profiles` patch is still accepted for step 3 (commissioning) and step 4 (maximum). Manual step 4 returns to step 3 after four hours (`max_level_remaining_seconds`). With six steps `profiles` sets extract/supply per step as before. `profiles`, `fan_levels` and `max_level` in the state always describe the steps the unit runs.

