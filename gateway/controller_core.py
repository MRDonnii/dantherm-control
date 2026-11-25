#!/usr/bin/env python3
"""Local controller for HCH5/HAC1 with modern automation and HCP4 arbitration."""
from __future__ import annotations

import copy
from collections import deque
import json
import math
import os
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

import fan_steps
import week_schedule
from fan_steps import MAX_LEVEL, MAX_LEVEL_HOURS, NOMINAL_LEVEL, STEP_COUNTS
from onewire_extras import clean_roles
from room_setup import RoomSetupError, clean_rooms
from afterheat_pilot import (
    CONTROL_MODES as AFTERHEAT_CONTROL_MODES,
    PI_BAND_DEFAULT,
    PI_BAND_RANGE,
    PI_GAIN_DEFAULT,
    PI_GAIN_RANGE,
    PI_INTEGRAL_MINUTES_DEFAULT,
    PI_INTEGRAL_MINUTES_RANGE,
    PI_MIN_DWELL_TICKS_DEFAULT,
    PI_MIN_DWELL_TICKS_RANGE,
    LEVEL_MAX as AFTERHEAT_LEVEL_MAX,
    AfterheatPilot,
    PilotInputs,
)
from afterheat_actuators import AlgorithmPilot
from afterheat_control import ALGORITHM_DEFAULT as AFTERHEAT_ALGORITHM_DEFAULT, ALGORITHMS as AFTERHEAT_ALGORITHMS
from night_cooling import NightCooling, OutdoorHistory
from room_cascade import SATURATION_MARGIN_K, RoomCascade
from advanced_control import (
    BALANCE_EXCESS_DEFAULT,
    BALANCE_RATIO_MODES,
    HCH5_MAX_AIRFLOW_M3H,
    absolute_humidity,
    airflow_plan,
    balanced_profiles,
    rpm_at_percent,
    fan_curve,
    side_constants,
    valid_source,
)

# HAC1 firmware lockout, confirmed by the owner 2026-09-23: the water
# afterheat never switches on while outdoor temperature (T1, register 180)
# is 15 C or higher, whatever the setpoint (even 35 C) and whoever is master.
# Register 209 staying 0 above this limit is correct HAC1 behaviour, not a
# Pi/RS485 fault - do not debug it. It is not configurable over RS485.
# NOT a guarantee: on 2026-09-27 HAC1 opened the water valve at 17.7 C
# outdoor (setpoint 35, fans stopped by bonfire). Only show it as a hint;
# never skip or relax afterheat control because of it.
AFTERHEAT_OUTDOOR_CUTOFF_C = 15.0

VALID_MODES = {"local_auto", "smart_auto", "manual"}
VALID_DEMANDS = {"low", "normal", "high", "boost"}
VALID_BYPASS = {"off", "on"}
VALID_QUICK_BOOST_MINUTES = {0, 15, 30, 60}
# Cool: bypass open and the top step for a while, then back to normal.
VALID_COOL_BOOST_MINUTES = {0, 30, 60, 120}
COOL_BOOST_AFTERHEAT_SETPOINT = 10
# Which indoor humidity the winter dry-air mode looks at.
WINTER_RH_BASES = ("average", "lowest")
VALID_AFTERHEAT_COILS = {"electric", "water"}
# Frost lock: the water afterheat coil sits in a cold attic. While it freezes
# the afterheat stays on (at least frost_lock_min_setpoint_c) whatever anybody
# asks for. It releases only when every known value has been above its limit
# plus the margin for the whole release time.
FROST_LOCK_RELEASE_MARGIN_C = 2.0
FROST_LOCK_RELEASE_SECONDS = 30 * 60
# A cold value must persist this long before the lock latches, so a single
# glitched reading (e.g. T1 = 0 after a bus fault) cannot lock in summer.
FROST_LOCK_ENGAGE_SECONDS = 120
# A short-lived afterheat setpoint written by a supervised experiment or tool:
# {"setpoint": 21.5, "expires_at": <unix time>}. Fractions are allowed (HAC1
# register 186 is setpoint*256). It never outlives AFTERHEAT_OVERRIDE_MAX_SECONDS,
# is ignored while the afterheat is off, and the frost floor still applies.
AFTERHEAT_OVERRIDE_PATH = Path(os.getenv("DANTHERM_AFTERHEAT_OVERRIDE", "/var/lib/dantherm-hch5-ha/afterheat-override.json"))
AFTERHEAT_OVERRIDE_MAX_SECONDS = 900
# While locked, the outdoor word in HAC1 register 180 never reaches HAC1's own
# 15 C summer lockout, so a stale or glitched T1 cannot lock the afterheat out.
FROST_LOCK_OUTDOOR_WORD_CAP_C = 10.0
# A failed afterheat write under the frost lock is retried at least this often.
FROST_LOCK_RETRY_MAX_SECONDS = 60.0
# Longest pause between retries of a failed hardware command.
RETRY_MAX_SECONDS = 300.0
# After HA's room lease runs out, Smart Auto keeps its last level this much
# longer before Local Auto takes over: a missed push or two must not flip the fan.
HA_LEASE_GRACE_SECONDS = 120
# How often a running fireplace mode is confirmed to the gateway (its timer is 15 min).
FIREPLACE_REFRESH_SECONDS = 60.0
FROST_LOCK_OFF_ERROR = "Frostsikring: eftervarmen kan ikke slukkes, mens det fryser."
FROST_LOCK_FLOOR_ERROR = "Frostsikring: eftervarmen kan ikke sættes under {floor} °C, mens det fryser."
FROST_LOCK_COOL_ERROR = "Frostsikring: Køl kan ikke startes, mens det fryser."
# Bonfire in the garden: the unit is switched off (standby pattern) for the
# chosen time so no smoke is drawn in, then starts again by itself.
BONFIRE_PROFILE = {"extract": 0, "supply": 0, "name": "Bål"}
BONFIRE_MINUTES_RANGE = (10, 480)
# Unit off (standby): both fans stopped with the HRC2/HCP4 standby pattern.
# -1 = until switched on again; otherwise 10 minutes to 7 days.
STANDBY_UNTIL_ON = -1
# -2 = until tomorrow morning at STANDBY_MORNING_HOUR (local time of the Pi).
STANDBY_UNTIL_MORNING = -2
STANDBY_MORNING_HOUR = 7
STANDBY_MINUTES_RANGE = (10, 7 * 24 * 60)
STANDBY_PROFILE = {"extract": 0, "supply": 0, "name": "Slukket"}

# Four Dantherm steps derived from the commissioning (fan_steps.py).
DEFAULT_PROFILES = fan_steps.ladder(fan_steps.DEFAULT_FAN_SETTINGS)
# Measured error of the 1-Wire T2 sensor (K to add); used for the lift, kWh and diagnostics only, never by the afterheat control.
T2_OFFSET_MEASURED_K = 1.5

DEFAULT_SCHEDULE = {
    str(day): {"enabled": True, "start": "07:00", "end": "22:00", "level": 3}
    for day in range(7)
}
DEFAULT_SCHEDULE["5"] = {"enabled": True, "start": "08:00", "end": "23:00", "level": 3}
DEFAULT_SCHEDULE["6"] = {"enabled": True, "start": "08:00", "end": "22:00", "level": 3}


class ControllerError(ValueError):
    pass


def _next_morning(now: float, hour: int) -> float:
    """Epoch of the next local HH:00: this morning after midnight, else tomorrow."""
    local = datetime.fromtimestamp(now).astimezone()
    target = local.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= local:
        target = (local + timedelta(days=1)).replace(hour=hour, minute=0, second=0, microsecond=0)
    return target.timestamp()


def _copy_profiles(profiles: dict | None = None) -> dict[int, dict[str, object]]:
    source = profiles or DEFAULT_PROFILES
    return {
        int(level): {
            "extract": int(values["extract"]),
            "supply": int(values["supply"]),
            "name": str(values.get("name") or f"Niveau {level}"),
        }
        for level, values in source.items()
    }


def _copy_schedule(schedule: dict | None = None) -> dict[str, dict[str, object]]:
    source = schedule or DEFAULT_SCHEDULE
    result: dict[str, dict[str, object]] = {}
    for day in range(7):
        values = source.get(str(day), source.get(day, DEFAULT_SCHEDULE[str(day)]))
        result[str(day)] = {
            "enabled": bool(values.get("enabled", True)),
            "start": str(values.get("start", "07:00")),
            "end": str(values.get("end", "22:00")),
            "level": int(values.get("level", 3)),
        }
    return result


def validate_ladder(profiles: dict[int, dict[str, object]]) -> None:
    try:
        fan_steps.validate_ladder(profiles)
    except ValueError as error:
        raise ControllerError(str(error)) from error


def clean_fan_settings(value: object, base: dict | None = None) -> dict[str, int]:
    try:
        return fan_steps.clean_fan_settings(value, base)
    except ValueError as error:
        raise ControllerError(str(error)) from error


def _validate_time(value: object, label: str) -> str:
    text = str(value or "")
    try:
        parsed = datetime.strptime(text, "%H:%M")
    except ValueError as error:
        raise ControllerError(f"{label} skal være HH:MM") from error
    return parsed.strftime("%H:%M")


def _parse_until(value: object, label: str) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if not math.isfinite(timestamp):
            raise ControllerError(f"{label} er ugyldig")
        return timestamp
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError as error:
        raise ControllerError(f"{label} skal være en gyldig dato/tid") from error


def _minutes(value: str) -> int:
    hour, minute = (int(part) for part in value.split(":", 1))
    return hour * 60 + minute


def _time_window_active(now: datetime, start: str, end: str) -> bool:
    current = now.hour * 60 + now.minute
    first, last = _minutes(start), _minutes(end)
    if first == last:
        return True
    if first < last:
        return first <= current < last
    return current >= first or current < last


def _danish_number(value: float, digits: int | None = None) -> str:
    """18 -> "18", 2.5 -> "2,5"; with digits a fixed number of decimals."""
    text = f"{value:.{digits}f}" if digits is not None else f"{float(value):g}"
    return text.replace(".", ",")


def _clean_afterheat_setpoint(value: object) -> int | float:
    """Afterheat setpoint 10..35 °C in 0.1 °C; whole degrees stay ints (older clients)."""
    number = min(35.0, max(10.0, round(float(value), 1)))
    return int(number) if number.is_integer() else number


def _whole_number(value: object) -> int:
    """A whole number from a settings patch; ValueError for fractions, booleans or anything else."""
    if isinstance(value, bool):
        raise ValueError("not a whole number")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        value = value.strip()
        try:
            return int(value)
        except ValueError:
            value = float(value)
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    raise ValueError("not a whole number")


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _round(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)


class HardwareAdapter:
    """Bindings to functions already verified on the physical installation."""

    def __init__(
        self,
        *,
        write_fan_pair: Callable[[int, int], object] | None = None,
        set_bypass: Callable[[str], object] | None = None,
        set_fireplace: Callable[[bool], object] | None = None,
        set_afterheat_setpoint: Callable[[int | None], object] | None = None,
        set_standby: Callable[[bool], object] | None = None,
    ) -> None:
        self.write_fan_pair = write_fan_pair
        self.set_bypass = set_bypass
        self.set_fireplace = set_fireplace
        self.set_afterheat_setpoint = set_afterheat_setpoint
        self.set_standby = set_standby


class ControllerState:
    """Persistent controller configuration and desired state."""

    DEFAULTS = {
        "enabled": True,
        "mode": "local_auto",
        "manual_level": 3,
        "local_normal_level": 3,
        "local_min_level": 1,
        "local_max_level": MAX_LEVEL,
        "air_quality_max_level": MAX_LEVEL,
        "rh_setpoint": 50.0,
        "rh_hysteresis": 3.0,
        "co2_setpoint": 800,
        "co2_hysteresis": 100,
        # Fixed correction added to the unit's own CO2 sensor (ppm), so it can be
        # matched to trusted room sensors. Applied before control and display.
        "co2_offset": 0,
        "auto_step_rh": 5.0,
        "auto_step_co2": 200,
        "downshift_delay_seconds": 300,
        # Smart Auto: a step up of one level waits until the demand has lasted
        # this long (0 = at once, as before). Jumps of two levels or more and
        # bathroom drying always go at once.
        "upshift_delay_seconds": 0,
        "boost_hold_seconds": 600,
        "ha_demand": "normal",
        "ha_requested_level": 3,
        "ha_reason": "No Home Assistant room data",
        "ha_last_seen": None,
        "ha_valid_for_seconds": 180,
        "ha_timeout_seconds": 300,
        "bypass": "off",
        "fireplace": False,
        "fireplace_until": None,
        "fireplace_duration_minutes": 0,
        "afterheat_setpoint": 20,
        "afterheat_enabled": True,
        # How the afterheater is drawn (electric element or water coil); no control effect.
        "afterheat_coil": "electric",
        "t3_setpoint": None,
        "t5_setpoint": None,
        "schedule_enabled": False,
        # Week planner periods; None = use the old single window per day.
        "schedule_periods": None,
        "night_enabled": False,
        "night_start": "22:00",
        "night_end": "06:00",
        "night_level": 2,
        "night_air_quality_max_level": NOMINAL_LEVEL,
        "bathroom_rh_setpoint": 65.0,
        "bathroom_rh_hysteresis": 5.0,
        "bathroom_max_level": MAX_LEVEL,
        # Shower detection: RH rising this many %-points within this many minutes
        # starts drying before the limit is reached.
        "bathroom_rise_percent": 7.0,
        "bathroom_rise_minutes": 10,
        # Drying may continue at night (the night level does not cap it).
        "bathroom_night_drying": True,
        # Safety if a sensor sticks high: stop drying after this many minutes (0 = no limit).
        "bathroom_max_minutes": 0,
        # Per room, set in the WebUI; wins over what Home Assistant sends:
        # {room: {"type": "auto|bathroom|normal", "rh_setpoint", "rh_hysteresis", "max_level"}}
        "bathroom_rooms": {},
        # Rooms set up on the WebUI page "Rum" (see room_setup.py): which Home
        # Assistant sensors belong to each room, its type, priority and control.
        "room_setup": [],
        # True once the rooms from the old HA options flow have been moved here.
        "room_setup_migrated": False,
        "vacation_enabled": False,
        "vacation_level": 1,
        "vacation_until": None,
        "vacation_from": None,
        "quick_boost_until": None,
        "quick_boost_level": MAX_LEVEL,
        "quick_boost_minutes": 0,
        "cool_boost_until": None,
        "cool_boost_minutes": 0,
        "bonfire_until": None,
        "bonfire_minutes": 0,
        "standby": False,
        "standby_until": None,
        "standby_minutes": 0,
        "cooling_enabled": False,
        "cooling_room_setpoint": 23.0,
        "cooling_hysteresis": 0.5,
        "cooling_outdoor_min": 12.0,
        "cooling_min_delta": 1.5,
        "cooling_level": NOMINAL_LEVEL,
        "cooling_start_delay_seconds": 180,
        "cooling_min_on_seconds": 600,
        "cooling_min_off_seconds": 300,
        "cooling_transition_timeout_seconds": 90,
        # House sizing: the base and minimum fan level follow from BR18.
        "sizing_enabled": False,
        "house_area_m2": 150.0,
        "ceiling_height_m": 2.5,
        "house_bathrooms": 1,
        "house_utility_rooms": 1,
        "airflow_max_m3h": HCH5_MAX_AIRFLOW_M3H,
        "airflow_measured": {},
        # Learned fan speed curve (rpm = rpm_at_0 + rpm_per_percent x %), set by the runtime.
        "fan_curve": None,
        "sizing_reduced_percent": 50,
        # Air balance: each level keeps its extract percentage and supply is
        # solved so extract stays this share above supply in m3/h.
        "balance_enabled": False,
        "balance_extract_excess_percent": BALANCE_EXCESS_DEFAULT,
        # "auto": duct ratio learned from the heat balance once trusted, else the fixed value.
        "balance_ratio_mode": "auto",
        "balance_duct_ratio": 1.0,
        # Heat-balance windows and the learned duct ratio (air_balance.py), set by the runtime.
        "balance_learned": None,
        # Afterheat supply target from the rooms (room_cascade: a slow outer PI
        # around the afterheat control). Off until the owner switches it on.
        "afterheat_room_enabled": False,
        "afterheat_room_target": 22.0,
        "afterheat_room_gain": 1.5,
        "afterheat_room_integral_minutes": 180.0,
        "afterheat_room_max_step": 0.2,
        # Dead band around the room target: inside it the supply target is left alone.
        "afterheat_room_band_k": 0.3,
        # Duct loss: added to the supply target so the air reaching the rooms is what the rooms
        # need (the supply duct cools the air in a cold loft). Not duct_rooms_offset_k, which
        # only corrects the room sensors in the extract duct loss figure.
        "afterheat_room_offset_k": 0.0,
        "afterheat_room_min": 17,
        "afterheat_room_max": 24,
        "afterheat_room_step_minutes": 10,
        "afterheat_room_source": "auto",
        # Weigh the rooms by their priority (low ½, high 1½, critical 2) in the average.
        "afterheat_room_weighted": False,
        # Set once the 1.12 defaults (22 °C room target) were applied to an unused older setting.
        "afterheat_room_v2": True,
        # The outer loop's last supply target, so a restart continues from it (set by the engine).
        "afterheat_room_value": None,
        # Night cooling from the weather forecast (night_cooling.py).
        "night_cool_enabled": False,
        # Off: on six steps the top level chosen by hand has no end time.
        "top_level_limit_enabled": False,
        "night_cool_start": "23:00",
        "night_cool_end": "07:00",
        "night_cool_forecast_c": 24.0,
        "night_cool_today_c": 22.0,
        "night_cool_season_c": 12.0,
        "night_cool_delta_k": 2.0,
        "night_cool_min_indoor_c": 20.5,
        "night_cool_level": 4,
        # Completed daily outdoor means [[date, mean, hours], ...] for the cooling season (set by the engine).
        "outdoor_daily_means": [],
        # Frost lock for a water afterheat coil (see FROST_LOCK_*): T1 or the
        # attic at or below frost_lock_outdoor_c, or the water-side frost
        # sensor at or below frost_lock_coil_c, keeps the afterheat on.
        "frost_lock_enabled": True,
        "frost_lock_outdoor_c": 3.0,
        "frost_lock_coil_c": 8.0,
        "frost_lock_min_setpoint_c": 18.0,
        # Set by the engine only: it has been freezing and has not thawed for
        # long enough yet. Saved so a restart without readings stays locked.
        "frost_lock_latched": False,
        # Who steers the afterheat level: HAC1 alone ("native") or the Pi's
        # PI controller through HAC1's whole-degree setpoint ("pi"), see afterheat_pilot.
        "afterheat_control_mode": "native",
        "afterheat_pi_gain": PI_GAIN_DEFAULT,
        "afterheat_pi_integral_minutes": PI_INTEGRAL_MINUTES_DEFAULT,
        # Calm operation: no level change while the air is this close to the
        # target (K), and a level is kept this many HAC1 ticks unless urgent.
        "afterheat_pi_band": PI_BAND_DEFAULT,
        "afterheat_pi_min_dwell_ticks": PI_MIN_DWELL_TICKS_DEFAULT,
        # Which algorithm chooses the level while the Pi steers (afterheat_control.ALGORITHMS);
        # "standard" is the pilot above, unchanged.
        "afterheat_algorithm": AFTERHEAT_ALGORITHM_DEFAULT,
        # Fireplace mode held by a stove sensor or an external switch.
        "fireplace_auto_enabled": False,
        "fireplace_auto_source": "",
        "fireplace_auto_on_temp": 25.0,
        "fireplace_auto_off_temp": 24.0,
        "fireplace_afterrun_minutes": 15,
        "fireplace_max_hours": 6,
        # Stop fireplace mode (and refuse to start it) while the air after the
        # afterheat coil stays below FIREPLACE_COLD_SUPPLY_C: with the extract
        # fan stopped there is no heat recovery. Off until chosen.
        "fireplace_cold_guard_enabled": False,
        # A humidity rise (shower, cooking) only boosts when the room RH is at
        # least this high; 0 = any RH (as before). Saves moisture in dry air.
        "humidity_rise_min_rh": 0.0,
        # Humidity demand only counts when outdoor air actually dries the house.
        "humidity_smart_enabled": False,
        "outdoor_humidity_source": "",
        # HA room (control off) whose temperature is the average of the rooms
        # with an extract valve; compared with T3 it shows the extract duct loss.
        "duct_extract_source": "",
        # Known sensor errors (measured against a reference) for the duct loss
        # only; nothing else uses them.
        "duct_rooms_offset_k": 0.0,
        "duct_t3_offset_k": 0.0,
        "t2_offset_k": 0.0,
        # The T2 error was measured once (1.13.0-beta.13): see ControllerConfig.load.
        "t2_offset_calibrated": False,
        "humidity_margin_gm3": 0.5,
        "dry_protection_enabled": False,
        # Dry-air limit: caps the level at dry_max_level while the Home
        # Assistant rooms (winter_rh_basis: average or lowest) are below
        # dry_rh_limit. It ends at limit + winter_rh_hysteresis, and every
        # decision holds for at least dry_hold_minutes (high CO2 ends it at
        # once). The unit's own RH is never used: it reads far too low.
        "dry_rh_limit": 30.0,
        "dry_max_level": 2,
        "dry_hold_minutes": 10,
        # Dry-air release in steps (off by default): once the dry-air limit has
        # let go while the rooms are still dry, CO2 lifts the level one step
        # per dry_release_step_ppm over its setpoint, from dry_max_level + 1,
        # instead of jumping straight to the full CO2 level. Humidity demand
        # (bathroom drying, humid rooms) is never held back.
        "dry_release_steps_enabled": False,
        "dry_release_step_ppm": 200,
        # Dynamic dry-air protection (1.13.0-beta.16, off by default; needs
        # dry_protection_enabled). In place of the single dry_rh_limit and the
        # winter mode's "T1 <= 3 C" gate: below dry_tier1_rh the level is capped
        # at dry_tier1_level, below dry_tier2_rh at dry_tier2_level, above it
        # normal demand control. A tier ends at its limit + dry_dynamic_rh_hysteresis.
        # It only limits while the outdoor air really dries the house: indoor
        # minus outdoor absolute humidity at least dry_ah_margin_gm3 (it stops
        # under margin - dry_ah_hysteresis_gm3, and always at 0 or less). The
        # cap never goes under max(dry_min_level, local_min_level, the house
        # sizing minimum). While it is on, the winter mode steps aside.
        "dry_dynamic_enabled": False,
        # Defaults for this 6-step unit: level 1 (supply 7 % / extract 25 %) is
        # badly unbalanced (underpressure with a wood stove), so never under 2.
        "dry_tier1_rh": 32.0,
        "dry_tier1_level": 2,
        "dry_tier2_rh": 36.0,
        "dry_tier2_level": 3,
        "dry_dynamic_rh_hysteresis": 2.5,
        "dry_ah_margin_gm3": 0.5,
        "dry_ah_hysteresis_gm3": 0.3,
        "dry_min_level": 2,
        # Optional (off): while the dynamic protection holds, CO2 over its
        # setpoint lifts at most one step over the tier cap; only CO2 over
        # dry_co2_release_ppm releases it (again with co2_hysteresis).
        "dry_co2_soft_enabled": False,
        "dry_co2_release_ppm": 1200,
        # Vacation lowers the afterheat target by vacation_afterheat_drop_k
        # while it runs (off by default). Only the target changes: the
        # afterheat control and the frost floor work as always.
        "vacation_afterheat_enabled": False,
        "vacation_afterheat_drop_k": 2.0,
        # Free cooling holds the afterheat down like Køl (off by default), so
        # the bypass air is not heated up again. The frost lock still wins.
        "cooling_afterheat_hold_enabled": False,
        # Winter dry-air mode: when the outdoor air (T1) is cold and the indoor
        # air is dry, the base level is lowered to winter_level. CO2, humidity
        # demand, bathroom drying, boost, manual control and the house minimum
        # still win. Start: T1 <= winter_outdoor_max and indoor RH below
        # winter_rh_limit. Stop: T1 above max + hysteresis or RH at limit +
        # hysteresis. A change holds for at least winter_hold_minutes.
        "winter_dry_enabled": False,
        "winter_outdoor_max": 3.0,
        "winter_outdoor_hysteresis": 2.0,
        "winter_rh_limit": 38.0,
        "winter_rh_hysteresis": 3.0,
        # "average" or "lowest" of the Home Assistant rooms (bathrooms left
        # out); without HA rooms the unit's own extract RH is used.
        "winter_rh_basis": "average",
        "winter_level": 2,
        "winter_hold_minutes": 30,
        # Fine dust (PM2.5) from Home Assistant room sensors; off until chosen.
        "pm25_enabled": False,
        "pm25_setpoint": 25.0,
        "pm25_step": 15.0,
        "pm25_hysteresis": 5.0,
        "pm25_max_level": NOMINAL_LEVEL,
        "pm25_ignored_rooms": [],
        # Extra DS18B20 sensors on the Pi's 1-Wire bus: {sensor_id: {"role", "name"}}.
        "onewire_roles": {},
        # Step control: 4 = Dantherm's four steps, 6 = the free six-step table.
        "fan_step_count": 4,
        # Four steps: commissioning as on the HCP4/HRC2: step-3 gear per fan,
        # the offset down to steps 2 and 1 and the step-4 maximum per fan.
        "fan_settings": dict(fan_steps.DEFAULT_FAN_SETTINGS),
        # Six steps: extract/supply gear per step, kept while four steps run.
        "six_step_profiles": None,
        # Measured airflow of the step control not in use (it belongs to its steps).
        "airflow_measured_4": {},
        "airflow_measured_6": {},
        # Four steps: step 4 chosen by hand runs for MAX_LEVEL_HOURS, then step 3.
        "max_level_until": None,
        # What the upgrade from six steps kept.
        "fan_steps_migration": None,
        "effective_source": "local_auto",
        "effective_level": 3,
        "effective_reason": "Controller starting",
        "updated_at": 0.0,
    }

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or os.getenv("DANTHERM_CONTROLLER_STATE", "/var/lib/dantherm-hch5-ha/controller.json"))
        self.lock = threading.RLock()
        self.balance_error: str | None = None
        # Base level set by the engine's winter dry-air mode (never saved).
        self.winter_base: int | None = None
        self.data = dict(self.DEFAULTS)
        self.data["profiles"] = _copy_profiles()
        self.data["six_step_profiles"] = _copy_profiles(fan_steps.SIX_DEFAULT_PROFILES)
        self.data["schedule"] = _copy_schedule()
        self.data["updated_at"] = time.time()
        self.load()

    def load(self) -> None:
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return
        if not isinstance(saved, dict):
            return
        try:
            saved, migrated = fan_steps.migrate_six_steps(saved)
        except (KeyError, TypeError, ValueError):
            migrated = False
        if migrated:
            backup = self.path.with_name(self.path.name + ".six-steps.bak")
            try:
                if not backup.exists():
                    backup.write_text(self.path.read_text(encoding="utf-8"), encoding="utf-8")
            except OSError:
                pass
        for key in self.DEFAULTS:
            if key in saved:
                self.data[key] = saved[key]
        if not saved.get("afterheat_room_v2"):
            # The room target was never used before 1.12: give it the new defaults.
            if not saved.get("afterheat_room_enabled"):
                self.data.update(afterheat_room_target=22.0, afterheat_room_min=17, afterheat_room_max=24,
                                 afterheat_room_step_minutes=10)
            self.data["afterheat_room_v2"] = True
        if not saved.get("t2_offset_calibrated"):
            # The 1-Wire T2 reads about 1.5 K below the air that the extract-side heat balance gives
            # (1148 minutes, p5-p95 1.3-2.3 K, the same at every afterheat level) and that the unit's
            # own T2AH confirms when the coil is cold (0.07 K median over 17 days without water).
            # The earlier correction of 0.4 K was too small: raise exactly that value to the measured
            # 1.5 K, once. Any other value is the owner's own choice and is kept.
            if abs(float(self.data.get("t2_offset_k") or 0.0) - 0.4) < 1e-9:
                self.data["t2_offset_k"] = T2_OFFSET_MEASURED_K
                migrated = True
            self.data["t2_offset_calibrated"] = True
            migrated = True
        if "air_quality_max_level" not in saved:
            # An existing six-step installation must keep its full range.
            self.data["air_quality_max_level"] = self.max_level()
        if isinstance(saved.get("schedule"), dict):
            try:
                self.data["schedule"] = _copy_schedule(saved["schedule"])
            except (TypeError, ValueError):
                pass
        self._sanitize()
        self._rebalance()
        if migrated:
            self.save()

    def _sanitize(self) -> None:
        if self.data["mode"] not in VALID_MODES:
            self.data["mode"] = "local_auto"
        if self.data.get("afterheat_coil") not in VALID_AFTERHEAT_COILS:
            self.data["afterheat_coil"] = "electric"
        if self.data.get("fan_step_count") not in STEP_COUNTS:
            self.data["fan_step_count"] = 4
        top = self.max_level()
        try:
            six = _copy_profiles(self.data.get("six_step_profiles") or fan_steps.SIX_DEFAULT_PROFILES)
            validate_ladder(six)
            if len(six) != 6:
                raise ControllerError("six")
        except (ControllerError, KeyError, TypeError, ValueError, AttributeError):
            six = _copy_profiles(fan_steps.SIX_DEFAULT_PROFILES)
        self.data["six_step_profiles"] = six
        for key, default in (
            ("manual_level", 3), ("local_normal_level", 3),
            ("local_min_level", 1), ("local_max_level", top),
            ("air_quality_max_level", top),
            ("ha_requested_level", 3), ("night_level", 2),
            ("night_air_quality_max_level", NOMINAL_LEVEL), ("bathroom_max_level", top),
            ("vacation_level", 1), ("quick_boost_level", top), ("cooling_level", NOMINAL_LEVEL),
        ):
            try:
                value = int(self.data[key])
            except (TypeError, ValueError):
                value = default
            self.data[key] = min(top, max(1, value))
        if self.data["local_min_level"] > self.data["local_max_level"]:
            self.data["local_min_level"], self.data["local_max_level"] = 1, top
        self.data["local_normal_level"] = min(self.data["local_max_level"], max(self.data["local_min_level"], self.data["local_normal_level"]))
        self.data["ha_requested_level"] = min(self.data["local_max_level"], max(self.data["local_min_level"], self.data["ha_requested_level"]))
        try:
            self.data["fan_settings"] = clean_fan_settings(self.data.get("fan_settings") or {})
        except ControllerError:
            self.data["fan_settings"] = dict(fan_steps.DEFAULT_FAN_SETTINGS)
        try:
            max_until = float(self.data["max_level_until"]) if self.data.get("max_level_until") else None
        except (TypeError, ValueError):
            max_until = None
        if self.top_hold_active():
            # Step 4 by hand always has an end, also for files without one.
            self.data["max_level_until"] = max_until or time.time() + MAX_LEVEL_HOURS * 3600
        else:
            self.data["max_level_until"] = None
        if self.data["bypass"] not in VALID_BYPASS:
            self.data["bypass"] = "off"
        if self.data["fireplace"] and self.data["bypass"] == "on":
            self.data["bypass"] = "off"
        try:
            fireplace_until = float(self.data["fireplace_until"]) if self.data.get("fireplace_until") else None
        except (TypeError, ValueError):
            fireplace_until = None
        if fireplace_until is None or fireplace_until <= time.time():
            self.data["fireplace"] = False
            self.data["fireplace_until"] = None
            self.data["fireplace_duration_minutes"] = 0
        else:
            self.data["fireplace"] = True
            self.data["fireplace_until"] = fireplace_until
            if self.data.get("fireplace_duration_minutes") not in (15, 30):
                self.data["fireplace_duration_minutes"] = 15
        if self.data["ha_demand"] not in VALID_DEMANDS:
            self.data["ha_demand"] = "normal"
        self.data["enabled"] = True
        try:
            self.data["ha_valid_for_seconds"] = min(900, max(30, int(self.data.get("ha_valid_for_seconds", 180))))
        except (TypeError, ValueError):
            self.data["ha_valid_for_seconds"] = 180
        self.data["ha_reason"] = str(self.data.get("ha_reason") or "No Home Assistant room data")[:160]
        try:
            self.data["afterheat_setpoint"] = _clean_afterheat_setpoint(self.data.get("afterheat_setpoint", 20))
        except (TypeError, ValueError):
            self.data["afterheat_setpoint"] = 20
        self.data["afterheat_enabled"] = bool(self.data.get("afterheat_enabled", True))
        for key in ("t3_setpoint", "t5_setpoint"):
            try:
                value = self.data.get(key)
                self.data[key] = None if value is None else (int(value) if 10 <= int(value) <= 35 else None)
            except (TypeError, ValueError):
                self.data[key] = None
        for key in ("schedule_enabled", "night_enabled", "vacation_enabled", "cooling_enabled"):
            self.data[key] = bool(self.data.get(key, False))
        try:
            self.data["night_start"] = _validate_time(self.data.get("night_start"), "Nat start")
            self.data["night_end"] = _validate_time(self.data.get("night_end"), "Nat slut")
        except ControllerError:
            self.data["night_start"], self.data["night_end"] = "22:00", "06:00"
        try:
            _parse_until(self.data.get("vacation_until"), "Ferie slut")
        except ControllerError:
            self.data["vacation_until"] = None
        try:
            _parse_until(self.data.get("vacation_from"), "Ferie start")
        except ControllerError:
            self.data["vacation_from"] = None
        if self.data.get("schedule_periods") is not None:
            try:
                self.data["schedule_periods"] = week_schedule.normalize(self.data["schedule_periods"], self.max_level())
            except week_schedule.ScheduleError:
                self.data["schedule_periods"] = None
        try:
            quick_boost_until = _parse_until(self.data.get("quick_boost_until"), "Quick Boost slut")
        except ControllerError:
            quick_boost_until = None
        try:
            quick_boost_minutes = int(self.data.get("quick_boost_minutes", 0))
        except (TypeError, ValueError):
            quick_boost_minutes = 0
        if quick_boost_minutes not in VALID_QUICK_BOOST_MINUTES or not quick_boost_until or quick_boost_until <= time.time():
            self.data["quick_boost_until"] = None
            self.data["quick_boost_minutes"] = 0
        else:
            self.data["quick_boost_until"] = quick_boost_until
            self.data["quick_boost_minutes"] = quick_boost_minutes
        try:
            cool_boost_until = _parse_until(self.data.get("cool_boost_until"), "Køl slut")
        except ControllerError:
            cool_boost_until = None
        try:
            cool_boost_minutes = int(self.data.get("cool_boost_minutes", 0))
        except (TypeError, ValueError):
            cool_boost_minutes = 0
        if cool_boost_minutes not in VALID_COOL_BOOST_MINUTES or not cool_boost_until or cool_boost_until <= time.time():
            self.data["cool_boost_until"] = None
            self.data["cool_boost_minutes"] = 0
        else:
            self.data["cool_boost_until"] = cool_boost_until
            self.data["cool_boost_minutes"] = cool_boost_minutes
        try:
            bonfire_until = _parse_until(self.data.get("bonfire_until"), "Bål slut")
        except ControllerError:
            bonfire_until = None
        if not bonfire_until or bonfire_until <= time.time():
            self.data["bonfire_until"] = None
            self.data["bonfire_minutes"] = 0
        else:
            self.data["bonfire_until"] = bonfire_until
        try:
            standby_until = _parse_until(self.data.get("standby_until"), "Standby slut")
        except ControllerError:
            standby_until = None
        self.data["standby"] = self.data.get("standby") is True
        if not self.data["standby"] or (standby_until is not None and standby_until <= time.time()):
            self.data["standby"], self.data["standby_until"], self.data["standby_minutes"] = False, None, 0
        else:
            self.data["standby_until"] = standby_until
        for key, default, low, high in (
            ("bathroom_rh_setpoint", 65.0, 35.0, 90.0),
            ("bathroom_rh_hysteresis", 5.0, 1.0, 20.0),
            ("cooling_room_setpoint", 23.0, 18.0, 30.0),
            ("cooling_hysteresis", 0.5, 0.2, 3.0),
            ("cooling_outdoor_min", 12.0, -10.0, 25.0),
            ("cooling_min_delta", 1.5, 0.5, 10.0),
        ):
            try:
                self.data[key] = min(high, max(low, float(self.data.get(key, default))))
            except (TypeError, ValueError):
                self.data[key] = default
        for key, default, low, high in (
            ("cooling_start_delay_seconds", 180, 0, 1800),
            ("cooling_min_on_seconds", 600, 0, 3600),
            ("cooling_min_off_seconds", 300, 0, 3600),
            ("cooling_transition_timeout_seconds", 90, 30, 300),
        ):
            try:
                self.data[key] = min(high, max(low, int(self.data.get(key, default))))
            except (TypeError, ValueError):
                self.data[key] = default
        schedule = _copy_schedule(self.data.get("schedule"))
        for values in schedule.values():
            try:
                values["start"] = _validate_time(values["start"], "Tidsplan start")
                values["end"] = _validate_time(values["end"], "Tidsplan slut")
                values["level"] = min(top, max(1, int(values["level"])))
                values["enabled"] = bool(values["enabled"])
            except (ControllerError, TypeError, ValueError):
                values.update(enabled=True, start="07:00", end="22:00", level=3)
        self.data["schedule"] = schedule
        self._sanitize_advanced()

    BOOL_KEYS = ("sizing_enabled", "afterheat_room_enabled", "fireplace_auto_enabled",
                 "humidity_smart_enabled", "dry_protection_enabled", "balance_enabled", "pm25_enabled",
                 "bathroom_night_drying", "winter_dry_enabled", "frost_lock_enabled",
                 "afterheat_room_weighted", "night_cool_enabled", "fireplace_cold_guard_enabled",
                 "top_level_limit_enabled", "dry_release_steps_enabled", "vacation_afterheat_enabled",
                 "cooling_afterheat_hold_enabled", "dry_dynamic_enabled",
                 "dry_co2_soft_enabled")
    TIME_KEYS = (("night_cool_start", "Natkøling start", "23:00"), ("night_cool_end", "Natkøling slut", "07:00"))

    def _sanitize_advanced(self) -> None:
        for key in self.BOOL_KEYS:
            self.data[key] = bool(self.data.get(key, False))
        self.data["frost_lock_latched"] = self.data.get("frost_lock_latched") is True
        self.data["afterheat_room_v2"] = self.data.get("afterheat_room_v2") is True
        value = self.data.get("afterheat_room_value")
        self.data["afterheat_room_value"] = (
            round(float(value), 2) if _is_number(value) and 10 <= float(value) <= 35 else None)
        self.data["outdoor_daily_means"] = OutdoorHistory.clean(self.data.get("outdoor_daily_means"))
        for key, label, default in self.TIME_KEYS:
            try:
                self.data[key] = _validate_time(self.data.get(key), label)
            except ControllerError:
                self.data[key] = default
        if self.data.get("balance_ratio_mode") not in BALANCE_RATIO_MODES:
            self.data["balance_ratio_mode"] = "auto"
        if self.data.get("winter_rh_basis") not in WINTER_RH_BASES:
            self.data["winter_rh_basis"] = "average"
        if self.data.get("afterheat_control_mode") not in AFTERHEAT_CONTROL_MODES:
            self.data["afterheat_control_mode"] = "native"
        if self.data.get("afterheat_algorithm") not in AFTERHEAT_ALGORITHMS:
            self.data["afterheat_algorithm"] = AFTERHEAT_ALGORITHM_DEFAULT
        self.data["balance_learned"] = self._clean_learned(self.data.get("balance_learned"))
        for key, low, high in self.ADVANCED_FLOATS:
            try:
                self.data[key] = min(high, max(low, float(self.data.get(key, self.DEFAULTS[key]))))
            except (TypeError, ValueError):
                self.data[key] = self.DEFAULTS[key]
        for key, low, high in self.ADVANCED_INTS:
            high = self.max_level() if high is None else high  # step choices
            try:
                self.data[key] = min(high, max(low, int(self.data.get(key, self.DEFAULTS[key]))))
            except (TypeError, ValueError):
                self.data[key] = self.DEFAULTS[key]
        if self.data["afterheat_room_min"] > self.data["afterheat_room_max"]:
            self.data["afterheat_room_min"], self.data["afterheat_room_max"] = 17, 24
        if self.data["fireplace_auto_off_temp"] >= self.data["fireplace_auto_on_temp"]:
            self.data["fireplace_auto_off_temp"] = self.data["fireplace_auto_on_temp"] - 1.0
        if self.data["dry_tier1_rh"] >= self.data["dry_tier2_rh"]:
            self.data["dry_tier1_rh"], self.data["dry_tier2_rh"] = 30.0, 35.0
        if self.data["dry_tier1_level"] > self.data["dry_tier2_level"]:
            self.data["dry_tier1_level"] = self.data["dry_tier2_level"]
        for key, allow_fixed, default in (
            ("afterheat_room_source", True, "auto"),
            ("fireplace_auto_source", False, ""),
            ("outdoor_humidity_source", False, ""),
            ("duct_extract_source", False, ""),
        ):
            try:
                self.data[key] = valid_source(self.data.get(key), allow_fixed=allow_fixed,
                                               allow_weather=key == "outdoor_humidity_source") or default
            except ValueError:
                self.data[key] = default
        try:
            self.data["airflow_measured"] = self._stamp_airflow(self._clean_airflow(self.data.get("airflow_measured")))
        except ControllerError:
            self.data["airflow_measured"] = {}
        try:
            self.data["onewire_roles"] = clean_roles(self.data.get("onewire_roles"))
        except ValueError:
            self.data["onewire_roles"] = {}
        try:
            self.data["pm25_ignored_rooms"] = self._clean_room_names(self.data.get("pm25_ignored_rooms"))
        except ControllerError:
            self.data["pm25_ignored_rooms"] = []
        try:
            self.data["bathroom_rooms"] = self._clean_bathroom_rooms(self.data.get("bathroom_rooms"))
        except ControllerError:
            self.data["bathroom_rooms"] = {}
        try:
            self.data["room_setup"] = clean_rooms(self.data.get("room_setup"), max_level=self.max_level())
        except RoomSetupError:
            self.data["room_setup"] = []
        self.data["room_setup_migrated"] = self.data.get("room_setup_migrated") is True

    ADVANCED_FLOATS = (
        ("house_area_m2", 20.0, 1000.0), ("ceiling_height_m", 1.8, 6.0),
        ("afterheat_room_target", 15.0, 26.0), ("afterheat_room_gain", 0.0, 5.0),
        ("afterheat_room_integral_minutes", 30.0, 600.0), ("afterheat_room_max_step", 0.1, 0.5),
        ("afterheat_room_band_k", 0.0, 1.5), ("afterheat_room_offset_k", -3.0, 3.0),
        ("night_cool_forecast_c", 15.0, 40.0), ("night_cool_today_c", 15.0, 40.0),
        ("night_cool_season_c", 0.0, 25.0), ("night_cool_delta_k", 0.5, 8.0),
        ("night_cool_min_indoor_c", 16.0, 26.0),
        ("fireplace_auto_on_temp", 15.0, 400.0), ("fireplace_auto_off_temp", 10.0, 399.0),
        ("humidity_margin_gm3", 0.0, 3.0), ("dry_rh_limit", 15.0, 45.0),
        ("dry_tier1_rh", 15.0, 45.0), ("dry_tier2_rh", 20.0, 50.0),
        ("dry_dynamic_rh_hysteresis", 1.0, 5.0),
        ("dry_ah_margin_gm3", 0.0, 3.0), ("dry_ah_hysteresis_gm3", 0.0, 2.0),
        ("vacation_afterheat_drop_k", 0.5, 8.0),
        ("balance_extract_excess_percent", 0.0, 20.0), ("balance_duct_ratio", 0.7, 1.5),
        ("duct_rooms_offset_k", -3.0, 3.0), ("duct_t3_offset_k", -3.0, 3.0),
        ("t2_offset_k", -3.0, 3.0),
        ("pm25_setpoint", 5.0, 200.0), ("pm25_step", 2.0, 100.0), ("pm25_hysteresis", 1.0, 50.0),
        ("bathroom_rise_percent", 3.0, 20.0), ("humidity_rise_min_rh", 0.0, 80.0),
        ("winter_outdoor_max", -25.0, 15.0), ("winter_outdoor_hysteresis", 0.5, 10.0),
        ("winter_rh_limit", 20.0, 55.0), ("winter_rh_hysteresis", 1.0, 10.0),
        ("frost_lock_outdoor_c", -10.0, 10.0), ("frost_lock_coil_c", 3.0, 15.0),
        ("frost_lock_min_setpoint_c", 15.0, 25.0),
        ("afterheat_pi_gain", *PI_GAIN_RANGE),
        ("afterheat_pi_integral_minutes", *PI_INTEGRAL_MINUTES_RANGE),
        ("afterheat_pi_band", *PI_BAND_RANGE),
    )
    ADVANCED_INTS = (
        ("house_bathrooms", 0, 10), ("house_utility_rooms", 0, 10),
        ("airflow_max_m3h", 100, 1500), ("sizing_reduced_percent", 30, 100),
        ("afterheat_room_min", 10, 35), ("afterheat_room_max", 10, 35),
        ("afterheat_room_step_minutes", 2, 60), ("fireplace_afterrun_minutes", 0, 120),
        ("fireplace_max_hours", 1, 24), ("dry_max_level", 1, None), ("pm25_max_level", 1, None),
        ("bathroom_rise_minutes", 3, 30), ("bathroom_max_minutes", 0, 720),
        ("winter_level", 1, None), ("winter_hold_minutes", 5, 240),
        ("dry_hold_minutes", 1, 240), ("dry_release_step_ppm", 50, 500),
        ("dry_tier1_level", 1, None), ("dry_tier2_level", 1, None), ("dry_min_level", 1, None),
        ("dry_co2_release_ppm", 600, 2000),
        ("afterheat_pi_min_dwell_ticks", *PI_MIN_DWELL_TICKS_RANGE),
        ("night_cool_level", 1, None),
    )

    @staticmethod
    def _clean_learned(value: object) -> dict[str, object] | None:
        """Heat-balance windows and the learned duct ratio; anything odd is dropped."""
        if not isinstance(value, dict):
            return None
        windows = []
        for window in value.get("windows") or []:
            try:
                windows.append({**window, "t": float(window["t"]), "duct_ratio": float(window["duct_ratio"]),
                                "delta_t": float(window["delta_t"])})
            except (KeyError, TypeError, ValueError):
                continue
        cleaned: dict[str, object] = {"windows": windows[-60:]}
        for key in ("ratio", "ratio_in_use", "spread", "idle_power_w", "updated_at"):
            try:
                cleaned[key] = None if value.get(key) is None else float(value[key])
            except (TypeError, ValueError):
                cleaned[key] = None
        ratio = cleaned["ratio_in_use"]
        if ratio is not None and not 0.7 <= ratio <= 1.5:
            cleaned["ratio_in_use"] = None
        for key in ("count", "nights"):
            try:
                cleaned[key] = int(value.get(key) or 0)
            except (TypeError, ValueError):
                cleaned[key] = 0
        cleaned["confidence"] = value.get("confidence") if value.get("confidence") in ("none", "low", "ok") else "none"
        cleaned["last"] = value.get("last") if isinstance(value.get("last"), dict) else None
        return cleaned

    def _clean_airflow(self, value: object) -> dict[str, dict[str, int]]:
        """Measured airflow per level (m3/h), each at the fan percentage it was measured at."""
        if value in (None, ""):
            return {}
        if not isinstance(value, dict):
            raise ControllerError("airflow_measured skal være et objekt")
        cleaned: dict[str, dict[str, int]] = {}
        for raw_level, values in value.items():
            try:
                level = int(raw_level)
            except (TypeError, ValueError) as error:
                raise ControllerError(f"Luftmængde kun for trin 1..{self.max_level()}") from error
            if not 1 <= level <= self.max_level() or not isinstance(values, dict):
                raise ControllerError(f"Luftmængde kun for trin 1..{self.max_level()}")
            entry: dict[str, int] = {}
            for side in ("supply", "extract"):
                raw = values.get(side)
                if raw in (None, "", 0):
                    continue
                try:
                    number = int(round(float(raw)))
                except (TypeError, ValueError) as error:
                    raise ControllerError("Luftmængde skal være et tal i m³/h") from error
                if not 10 <= number <= 1500:
                    raise ControllerError("Luftmængde skal være 10..1500 m³/h")
                entry[side] = number
                percent = values.get(f"{side}_percent")
                if percent not in (None, ""):
                    try:
                        percent = int(round(float(percent)))
                    except (TypeError, ValueError) as error:
                        raise ControllerError("Målt ved skal være en procent") from error
                    if not 1 <= percent <= 100:
                        raise ControllerError("Målt ved skal være 1..100 %")
                    entry[f"{side}_percent"] = percent
            if entry:
                cleaned[str(level)] = entry
        return cleaned

    def _stamp_airflow(self, measured: dict[str, dict[str, int]], previous: dict | None = None) -> dict[str, dict[str, int]]:
        """Tie each measured value to a fan percentage.

        A value keeps the percentage it had; a new or changed value without one
        belongs to the percentage the level runs now.
        """
        previous = previous if previous is not None else self.data.get("airflow_measured") or {}
        for level, entry in measured.items():
            old = previous.get(level) if isinstance(previous.get(level), dict) else {}
            profile = self.data["profiles"].get(int(level)) or {}
            for side in ("supply", "extract"):
                key = f"{side}_percent"
                if side not in entry or key in entry:
                    continue
                if old.get(side) == entry[side] and old.get(key) is not None:
                    entry[key] = int(old[key])
                elif profile.get(side) is not None:
                    entry[key] = int(profile[side])
        return measured

    def airflow_plan(self) -> dict[str, object]:
        return airflow_plan(self.data, self.data["profiles"])

    def set_fan_curve(self, curve: dict[str, object]) -> bool:
        """Store the fan speed curve learned from the unit (not user-settable).

        Returns True when the balance moved a supply percentage.
        """
        with self.lock:
            self.data["fan_curve"] = dict(curve)
            changed = self._rebalance()
            self.save()
            return changed

    def set_balance_learned(self, learned: dict[str, object]) -> bool:
        """Store the heat-balance windows and duct ratio (runtime only).

        Returns True when the balance moved a supply percentage.
        """
        with self.lock:
            self.data["balance_learned"] = self._clean_learned(learned)
            changed = self._rebalance()
            self.save()
            return changed

    def max_level(self) -> int:
        return 6 if self.data.get("fan_step_count") == 6 else MAX_LEVEL

    def dantherm_steps(self) -> bool:
        return self.max_level() == MAX_LEVEL

    def top_hold_active(self) -> bool:
        """The top level chosen by hand, which ends by itself after MAX_LEVEL_HOURS.

        Always on four Dantherm steps (like the HCP4 panel); on six steps only
        with the setting "top_level_limit_enabled" (off by default).
        """
        timed = self.dantherm_steps() or self.data.get("top_level_limit_enabled") is True
        return (timed and self.data.get("mode") == "manual"
                and self.data.get("manual_level") == self.max_level())

    def top_return_level(self) -> int:
        """Where the timed top level returns to: step 3 on four steps, else one step down."""
        return NOMINAL_LEVEL if self.dantherm_steps() else self.max_level() - 1

    def base_profiles(self) -> dict[int, dict[str, object]]:
        """Steps before the balance: the Dantherm ladder or the six-step table."""
        if self.dantherm_steps():
            return _copy_profiles(fan_steps.ladder(self.data["fan_settings"]))
        return _copy_profiles(self.data["six_step_profiles"])

    def _set_step_count(self, count: int) -> None:
        """Switch between four and six steps; every step choice follows."""
        if count not in STEP_COUNTS:
            raise ControllerError("Trinstyring skal være 4 eller 6 trin")
        if count == self.max_level():
            return
        old_profiles = _copy_profiles(self.data["profiles"])
        old_count = self.max_level()
        old_top_max = int(self.data["local_max_level"]) >= old_count
        old_top_air = int(self.data["air_quality_max_level"]) >= old_count
        self.data[f"airflow_measured_{old_count}"] = self.data.get("airflow_measured") or {}
        self.data["airflow_measured"] = self.data.get(f"airflow_measured_{count}") or {}
        self.data["fan_step_count"] = count
        self._rebalance()
        moved = fan_steps.remap_levels(self.data, old_profiles, self.data["profiles"])
        for key in fan_steps.LEVEL_KEYS + ("schedule", "schedule_periods"):
            if key in moved:
                self.data[key] = moved[key]
        # The Auto range follows like every other step choice; an Auto
        # maximum at the top step stays at the top step.
        if old_top_max:
            self.data["local_max_level"] = count
        if old_top_air:
            self.data["air_quality_max_level"] = count
        self.data["local_min_level"] = min(self.data["local_min_level"], self.data["local_max_level"])
        self.data["max_level_until"] = None

    def _rebalance(self, *, strict: bool = False) -> bool:
        """Build the four steps from the commissioning, balanced if chosen.

        Extract always follows the Dantherm ladder. Without the balance the
        supply fan does too; with it, supply on every step is solved so
        extract stays the chosen share above supply in m3/h. The profiles
        stay what the unit runs, so the engine, the airflow plan and Home
        Assistant need no second set. Returns True when a step changed.
        strict raises when no valid ladder comes out (a user change);
        otherwise the unbalanced Dantherm ladder is used.
        """
        profiles = self.base_profiles()
        self.balance_error = None
        if self.data.get("balance_enabled"):
            balanced = _copy_profiles(profiles)
            for level, values in balanced_profiles(self.data, balanced).items():
                balanced[level]["supply"] = int(values["supply"])
            try:
                validate_ladder(balanced)
                profiles = balanced
            except ControllerError as error:
                self.balance_error = f"Luftbalancen kan ikke lave gyldige trin: {error}"
                if strict:
                    raise ControllerError(self.balance_error) from error
        if not self.balance_error:
            try:
                validate_ladder(profiles)
            except ControllerError:
                if strict:
                    raise
        changed = profiles != self.data.get("profiles")
        self.data["profiles"] = profiles
        return changed

    def balance_status(self) -> dict[str, object]:
        """Air balance per level: what the balance gives and what runs now."""
        curve = fan_curve(self.data)
        k = side_constants(self.data)
        levels = balanced_profiles(self.data, self.data["profiles"])
        for level, values in levels.items():
            running = self.data["profiles"][level]
            extract_flow = float(k["extract"]) * rpm_at_percent(int(running["extract"]), curve)
            supply_flow = float(k["supply"]) * rpm_at_percent(int(running["supply"]), curve)
            values["current_supply"] = int(running["supply"])
            values["current_excess_percent"] = round((extract_flow / supply_flow - 1.0) * 100.0, 1)
        learned = self.data.get("balance_learned") or {}
        return {
            "enabled": self.data.get("balance_enabled") is True,
            "target_excess_percent": float(self.data["balance_extract_excess_percent"]),
            "duct_ratio": round(float(k["ratio"]), 3),
            "duct_ratio_source": k["source"],
            "measured_sides": k["measured"],
            "levels": levels,
            "learned": {key: learned.get(key) for key in (
                "ratio", "ratio_in_use", "spread", "count", "nights", "confidence", "updated_at", "last", "idle_power_w")},
            "error": self.balance_error,
        }

    def schedule_periods(self) -> dict[str, list[dict[str, object]]]:
        periods = self.data.get("schedule_periods")
        return periods if periods is not None else week_schedule.from_legacy(self.data.get("schedule"))

    def vacation_running(self, now: float | None = None) -> bool:
        if not self.data.get("vacation_enabled"):
            return False
        try:
            start = _parse_until(self.data.get("vacation_from"), "Ferie start")
        except ControllerError:
            start = None
        return start is None or start <= (now or time.time())

    def configured_normal_level(self) -> int:
        """Base level: from the house size when sizing is on, else the user's."""
        if self.data.get("sizing_enabled"):
            return int(self.airflow_plan()["base_level"])
        return int(self.data["local_normal_level"])

    def normal_level(self) -> int:
        """Base level in use: the configured one, lowered while winter mode holds."""
        base = self.configured_normal_level()
        if self.winter_base is not None and self.winter_base < base:
            return int(self.winter_base)
        return base

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".controller-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self.data, handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    REMOVED_KEYS = frozenset({
        "presence_away_enabled", "presence_away_level", "presence_busy_enabled", "presence_busy_rooms",
        "presence_on_minutes", "presence_off_minutes",
    })

    def configure(self, patch: dict[str, object]) -> dict[str, object]:
        with self.lock:
            backup = copy.deepcopy(self.data)
            try:
                return self._configure(patch)
            except ControllerError:
                self.data = backup
                raise
            except (TypeError, ValueError, OverflowError, AttributeError) as error:
                # A value of the wrong type (None, a list, "x", inf) is the
                # caller's mistake: answer 400, never drop the connection.
                self.data = backup
                field = next(iter(patch), "?") if len(patch) == 1 else "en af værdierne"
                raise ControllerError(f"Ugyldig værdi for {field}") from error
            except Exception:
                self.data = backup
                raise

    def _configure(self, patch: dict[str, object]) -> dict[str, object]:
        with self.lock:
            # What the frost lock compares a resent form against.
            before_afterheat = (self.data.get("afterheat_enabled"), float(self.data.get("afterheat_setpoint", 20)))
            allowed = {
                "mode", "manual_level", "local_normal_level", "local_min_level", "local_max_level", "air_quality_max_level",
                "rh_setpoint", "rh_hysteresis", "co2_setpoint", "co2_hysteresis", "co2_offset",
                "auto_step_rh", "auto_step_co2", "downshift_delay_seconds", "upshift_delay_seconds",
                "boost_hold_seconds", "ha_timeout_seconds", "bypass", "fireplace",
                "fireplace_minutes", "afterheat_setpoint", "afterheat_enabled", "afterheat_coil", "t3_setpoint", "t5_setpoint", "profiles", "fan_settings", "fan_step_count", "schedule_enabled",
                "schedule", "night_enabled", "night_start", "night_end", "night_level",
                "night_air_quality_max_level", "bathroom_rh_setpoint",
                "bathroom_rh_hysteresis", "bathroom_max_level",
                "vacation_enabled", "vacation_level", "vacation_until", "vacation_from", "schedule_periods",
                "quick_boost_minutes", "quick_boost_level", "cool_boost_minutes", "bonfire_minutes", "standby_minutes", "cooling_enabled",
                "cooling_room_setpoint", "cooling_hysteresis", "cooling_outdoor_min",
                "cooling_min_delta", "cooling_level", "cooling_start_delay_seconds",
                "cooling_min_on_seconds", "cooling_min_off_seconds", "cooling_transition_timeout_seconds",
                *self.ADVANCED_KEYS, "room_setup",
            }
            # Settings removed in a later version (occupancy, 1.12.2): dropped silently, e.g. from a cached WebUI.
            patch = {key: value for key, value in patch.items() if key not in self.REMOVED_KEYS}
            unknown = set(patch) - allowed
            if unknown:
                raise ControllerError(f"Ukendt controller-felt: {sorted(unknown)[0]}")
            if "fan_step_count" in patch:
                try:
                    self._set_step_count(int(patch["fan_step_count"]))
                except (TypeError, ValueError) as error:
                    raise ControllerError("Trinstyring skal være 4 eller 6 trin") from error
            top = self.max_level()
            was_max = self.data["mode"] == "manual" and self.data["manual_level"] == top
            if "mode" in patch:
                if patch["mode"] not in VALID_MODES:
                    raise ControllerError("Ugyldig controller-mode")
                self.data["mode"] = patch["mode"]
            for key in ("manual_level", "local_normal_level", "local_min_level", "local_max_level", "air_quality_max_level", "night_level", "night_air_quality_max_level", "bathroom_max_level", "vacation_level", "quick_boost_level", "cooling_level"):
                if key in patch:
                    value = int(patch[key])
                    if not 1 <= value <= top:
                        raise ControllerError(f"{key} skal være 1..{top}")
                    self.data[key] = value
            if self.top_hold_active() and (not was_max or "manual_level" in patch):
                # Choosing step 4 (again) starts its four hours.
                self.data["max_level_until"] = time.time() + MAX_LEVEL_HOURS * 3600
            for key, low, high in (
                ("rh_setpoint", 25.0, 80.0), ("rh_hysteresis", 1.0, 10.0),
                ("bathroom_rh_setpoint", 35.0, 90.0), ("bathroom_rh_hysteresis", 1.0, 20.0),
                ("auto_step_rh", 2.0, 20.0), ("cooling_room_setpoint", 18.0, 30.0),
                ("cooling_hysteresis", 0.2, 3.0), ("cooling_outdoor_min", -10.0, 25.0),
                ("cooling_min_delta", 0.5, 10.0),
            ):
                if key in patch:
                    value = float(patch[key])
                    if not low <= value <= high:
                        raise ControllerError(f"{key} udenfor gyldigt område")
                    self.data[key] = value
            for key, low, high in (
                ("co2_setpoint", 500, 2000), ("co2_hysteresis", 25, 500), ("co2_offset", -1000, 1000),
                ("auto_step_co2", 50, 1000), ("ha_timeout_seconds", 60, 3600),
                ("downshift_delay_seconds", 30, 3600), ("upshift_delay_seconds", 0, 1800),
                ("boost_hold_seconds", 60, 3600),
                ("cooling_start_delay_seconds", 0, 1800), ("cooling_min_on_seconds", 0, 3600),
                ("cooling_min_off_seconds", 0, 3600), ("cooling_transition_timeout_seconds", 30, 300),
            ):
                if key in patch:
                    value = int(patch[key])
                    if not low <= value <= high:
                        raise ControllerError(f"{key} udenfor gyldigt område")
                    self.data[key] = value
            for key in ("schedule_enabled", "night_enabled", "vacation_enabled", "cooling_enabled"):
                if key in patch:
                    if not isinstance(patch[key], bool):
                        raise ControllerError(f"{key} skal være boolean")
                    self.data[key] = patch[key]
            if "afterheat_coil" in patch:
                if patch["afterheat_coil"] not in VALID_AFTERHEAT_COILS:
                    raise ControllerError("afterheat_coil skal være electric eller water")
                self.data["afterheat_coil"] = patch["afterheat_coil"]
            for key, label in (("night_start", "Nat start"), ("night_end", "Nat slut")):
                if key in patch:
                    self.data[key] = _validate_time(patch[key], label)
            if "vacation_until" in patch:
                value = patch["vacation_until"]
                _parse_until(value, "Ferie slut")
                self.data["vacation_until"] = str(value)[:40] if value else None
            if "vacation_from" in patch:
                value = patch["vacation_from"]
                _parse_until(value, "Ferie start")
                self.data["vacation_from"] = str(value)[:40] if value else None
            if self.data.get("vacation_from") and self.data.get("vacation_until"):
                if _parse_until(self.data["vacation_until"], "Ferie slut") <= _parse_until(self.data["vacation_from"], "Ferie start"):
                    raise ControllerError("Ferie slut skal ligge efter ferie start")
            if "schedule_periods" in patch:
                try:
                    self.data["schedule_periods"] = (
                        None if patch["schedule_periods"] is None else week_schedule.normalize(patch["schedule_periods"], top))
                except week_schedule.ScheduleError as error:
                    raise ControllerError(str(error)) from error
            if "quick_boost_minutes" in patch:
                minutes = int(patch["quick_boost_minutes"])
                if minutes not in VALID_QUICK_BOOST_MINUTES:
                    raise ControllerError("Quick Boost skal være 0, 15, 30 eller 60 minutter")
                if minutes and self.data.get("fireplace"):
                    raise ControllerError("Quick Boost kan ikke startes under pejsefunktion")
                if minutes and self.data.get("standby"):
                    raise ControllerError("Anlægget er slukket; tænd det først")
                self.data["quick_boost_minutes"] = minutes
                self.data["quick_boost_until"] = time.time() + minutes * 60 if minutes else None
                if minutes:
                    self.data["bonfire_until"] = None
                    self.data["bonfire_minutes"] = 0
            if "cool_boost_minutes" in patch:
                minutes = int(patch["cool_boost_minutes"])
                if minutes not in VALID_COOL_BOOST_MINUTES:
                    raise ControllerError("Køl skal være 0, 30, 60 eller 120 minutter")
                if minutes and self.data.get("fireplace"):
                    raise ControllerError("Køl kan ikke startes under pejsefunktion")
                if minutes and self.data.get("standby"):
                    raise ControllerError("Anlægget er slukket; tænd det først")
                self.data["cool_boost_minutes"] = minutes
                self.data["cool_boost_until"] = time.time() + minutes * 60 if minutes else None
                if minutes:
                    self.data["bonfire_until"] = None
                    self.data["bonfire_minutes"] = 0
            if "standby_minutes" in patch:
                minutes = int(patch["standby_minutes"])
                low, high = STANDBY_MINUTES_RANGE
                if minutes not in (0, STANDBY_UNTIL_ON, STANDBY_UNTIL_MORNING) and not low <= minutes <= high:
                    raise ControllerError(f"Standby skal være {low}..{high} minutter, til i morgen eller indtil tændt")
                self.data["standby"] = minutes != 0
                self.data["standby_minutes"] = minutes
                if minutes == STANDBY_UNTIL_MORNING:
                    self.data["standby_until"] = _next_morning(time.time(), STANDBY_MORNING_HOUR)
                else:
                    self.data["standby_until"] = time.time() + minutes * 60 if minutes > 0 else None
                if minutes:
                    # Off means off: temporary functions end.
                    self.data["quick_boost_until"], self.data["quick_boost_minutes"] = None, 0
                    self.data["cool_boost_until"], self.data["cool_boost_minutes"] = None, 0
                    self.data["bonfire_until"], self.data["bonfire_minutes"] = None, 0
                    self.data["fireplace"], self.data["fireplace_until"] = False, None
                    self.data["fireplace_duration_minutes"] = 0
            if "bonfire_minutes" in patch:
                minutes = int(patch["bonfire_minutes"])
                low, high = BONFIRE_MINUTES_RANGE
                if minutes and not low <= minutes <= high:
                    raise ControllerError(f"Bål skal vare {low}..{high} minutter")
                if minutes and self.data.get("fireplace"):
                    raise ControllerError("Bål kan ikke startes under pejsefunktion")
                if minutes and self.data.get("standby"):
                    raise ControllerError("Anlægget er slukket; tænd det først")
                self.data["bonfire_minutes"] = minutes
                self.data["bonfire_until"] = time.time() + minutes * 60 if minutes else None
                if minutes:
                    self.data["quick_boost_until"] = None
                    self.data["quick_boost_minutes"] = 0
                    self.data["cool_boost_until"] = None
                    self.data["cool_boost_minutes"] = 0
            if "schedule" in patch:
                incoming = patch["schedule"]
                if not isinstance(incoming, dict):
                    raise ControllerError("schedule skal være et objekt")
                schedule = _copy_schedule(self.data["schedule"])
                for raw_day, values in incoming.items():
                    day = str(int(raw_day))
                    if day not in schedule or not isinstance(values, dict):
                        raise ControllerError("Tidsplan understøtter ugedag 0..6")
                    if "enabled" in values:
                        schedule[day]["enabled"] = bool(values["enabled"])
                    if "start" in values:
                        schedule[day]["start"] = _validate_time(values["start"], "Tidsplan start")
                    if "end" in values:
                        schedule[day]["end"] = _validate_time(values["end"], "Tidsplan slut")
                    if "level" in values:
                        level = int(values["level"])
                        if not 1 <= level <= top:
                            raise ControllerError(f"Tidsplan-niveau skal være 1..{top}")
                        schedule[day]["level"] = level
                self.data["schedule"] = schedule
            if "bypass" in patch:
                if patch["bypass"] not in VALID_BYPASS:
                    raise ControllerError("Bypass skal være off eller on")
                if patch["bypass"] == "on" and bool(patch.get("fireplace", self.data["fireplace"])):
                    raise ControllerError("Bypass kan ikke aktiveres under pejsefunktion")
                self.data["bypass"] = patch["bypass"]
            if "fireplace" in patch:
                if not isinstance(patch["fireplace"], bool):
                    raise ControllerError("fireplace skal være boolean")
                minutes = 15 if patch["fireplace"] else 0
                if minutes and self.data.get("standby"):
                    raise ControllerError("Anlægget er slukket; tænd det først")
                if minutes and self.data["bypass"] == "on":
                    raise ControllerError("Pejsefunktion kan ikke aktiveres mens bypass er tændt")
                self.data["fireplace"] = minutes > 0
                self.data["fireplace_until"] = time.time() + minutes * 60 if minutes else None
                self.data["fireplace_duration_minutes"] = minutes
                if minutes:
                    self.data["quick_boost_until"] = None
                    self.data["quick_boost_minutes"] = 0
                    self.data["cool_boost_until"] = None
                    self.data["cool_boost_minutes"] = 0
                    self.data["bonfire_until"] = None
                    self.data["bonfire_minutes"] = 0
            if "fireplace_minutes" in patch:
                minutes = int(patch["fireplace_minutes"])
                if minutes not in (0, 15, 30):
                    raise ControllerError("Pejsetid skal være 0, 15 eller 30 minutter")
                if minutes and self.data.get("standby"):
                    raise ControllerError("Anlægget er slukket; tænd det først")
                if minutes and self.data["bypass"] == "on":
                    raise ControllerError("Pejsefunktion kan ikke aktiveres mens bypass er tændt")
                self.data["fireplace"] = minutes > 0
                self.data["fireplace_until"] = time.time() + minutes * 60 if minutes else None
                self.data["fireplace_duration_minutes"] = minutes
                if minutes:
                    self.data["quick_boost_until"] = None
                    self.data["quick_boost_minutes"] = 0
                    self.data["cool_boost_until"] = None
                    self.data["cool_boost_minutes"] = 0
                    self.data["bonfire_until"] = None
                    self.data["bonfire_minutes"] = 0
            if "afterheat_setpoint" in patch:
                try:
                    value = float(patch["afterheat_setpoint"])
                except (TypeError, ValueError) as error:
                    raise ControllerError("Eftervarme-setpunkt skal være 10..35 °C") from error
                if isinstance(patch["afterheat_setpoint"], bool) or not 10 <= value <= 35:
                    raise ControllerError("Eftervarme-setpunkt skal være 10..35 °C")
                self.data["afterheat_setpoint"] = _clean_afterheat_setpoint(value)
                self.data["afterheat_enabled"] = True
            for key, label in (("t3_setpoint", "T3"), ("t5_setpoint", "T5")):
                if key in patch:
                    value = patch[key]
                    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or not 10 <= value <= 35):
                        raise ControllerError(f"{label}-setpunkt skal være 10..35 °C eller OFF")
                    self.data[key] = value
            if "afterheat_enabled" in patch:
                if not isinstance(patch["afterheat_enabled"], bool):
                    raise ControllerError("afterheat_enabled skal være boolean")
                self.data["afterheat_enabled"] = patch["afterheat_enabled"]
            if "fan_settings" in patch:
                self.data["fan_settings"] = clean_fan_settings(patch["fan_settings"], self.data["fan_settings"])
            if "profiles" in patch and not self.dantherm_steps():
                self._configure_six_steps(patch)
            elif "profiles" in patch:
                # Older clients edit steps directly: step 3 is the commissioning
                # and step 4 the maximum; steps 1 and 2 follow the offset.
                incoming = patch["profiles"]
                if not isinstance(incoming, dict):
                    raise ControllerError("profiles skal være et objekt")
                settings = dict(self.data["fan_settings"])
                for raw_level, values in incoming.items():
                    level = int(raw_level)
                    if not 1 <= level <= MAX_LEVEL or not isinstance(values, dict):
                        raise ControllerError("Kun trin 1..4 understøttes")
                    current = self.data["profiles"][level]
                    for side in ("supply", "extract"):
                        if side not in values or int(values[side]) == int(current[side]):
                            continue
                        if level < NOMINAL_LEVEL:
                            raise ControllerError("Trin 1 og 2 følger gearafstanden. Ændr trin 3 eller gearafstanden")
                        if side == "supply" and self.data.get("balance_enabled"):
                            raise ControllerError("Indblæsningen styres af luftbalancen. Ændr udsugningen, eller slå luftbalancen fra for at sætte indblæsningen selv")
                        settings[side if level == NOMINAL_LEVEL else f"max_{side}"] = int(values[side])
                if settings["max_supply"] < settings["supply"]:
                    settings["max_supply"] = settings["supply"]
                if settings["max_extract"] < settings["extract"]:
                    settings["max_extract"] = settings["extract"]
                self.data["fan_settings"] = clean_fan_settings(settings)
            if (not self.dantherm_steps() and patch.get("balance_enabled") is False
                    and self.data.get("balance_enabled")):
                # Six steps: switching the balance off keeps the balanced supply.
                self.data["six_step_profiles"] = _copy_profiles(self.data["profiles"])
            self._configure_advanced(patch)
            self._check_frost_lock(patch, *before_afterheat)
            self._sanitize()
            self._rebalance(strict=True)
            self.data["updated_at"] = time.time()
            self.save()
            return self.snapshot()

    ADVANCED_KEYS = (
        "sizing_enabled", "house_area_m2", "ceiling_height_m", "house_bathrooms",
        "house_utility_rooms", "airflow_max_m3h", "airflow_measured", "sizing_reduced_percent",
        "balance_enabled", "balance_extract_excess_percent", "balance_ratio_mode", "balance_duct_ratio",
        "balance_learning_reset",
        "afterheat_room_enabled", "afterheat_room_target", "afterheat_room_gain",
        "afterheat_room_min", "afterheat_room_max", "afterheat_room_step_minutes",
        "afterheat_room_source", "fireplace_auto_enabled", "fireplace_auto_source",
        "fireplace_auto_on_temp", "fireplace_auto_off_temp", "fireplace_afterrun_minutes",
        "fireplace_max_hours", "fireplace_cold_guard_enabled", "humidity_rise_min_rh", "humidity_smart_enabled", "outdoor_humidity_source", "duct_extract_source",
        "duct_rooms_offset_k", "duct_t3_offset_k",
        "t2_offset_k",
        "humidity_margin_gm3", "dry_protection_enabled", "dry_rh_limit", "dry_max_level",
        "dry_hold_minutes", "dry_release_steps_enabled", "dry_release_step_ppm",
        "dry_dynamic_enabled", "dry_tier1_rh", "dry_tier1_level", "dry_tier2_rh", "dry_tier2_level",
        "dry_dynamic_rh_hysteresis", "dry_ah_margin_gm3", "dry_ah_hysteresis_gm3", "dry_min_level",
        "dry_co2_soft_enabled", "dry_co2_release_ppm",
        "vacation_afterheat_enabled", "vacation_afterheat_drop_k", "cooling_afterheat_hold_enabled",
        "onewire_roles", "pm25_enabled", "pm25_setpoint", "pm25_step", "pm25_hysteresis",
        "pm25_max_level", "pm25_ignored_rooms",
        "bathroom_rise_percent", "bathroom_rise_minutes", "bathroom_night_drying",
        "bathroom_max_minutes", "bathroom_rooms",
        "winter_dry_enabled", "winter_outdoor_max", "winter_outdoor_hysteresis", "winter_rh_limit",
        "winter_rh_hysteresis", "winter_rh_basis", "winter_level", "winter_hold_minutes",
        "frost_lock_enabled", "frost_lock_outdoor_c", "frost_lock_coil_c", "frost_lock_min_setpoint_c",
        "afterheat_control_mode", "afterheat_pi_gain", "afterheat_pi_integral_minutes",
        "afterheat_pi_band", "afterheat_pi_min_dwell_ticks", "afterheat_algorithm",
        "afterheat_room_integral_minutes", "afterheat_room_max_step", "afterheat_room_weighted",
        "afterheat_room_band_k", "afterheat_room_offset_k",
        "night_cool_enabled", "top_level_limit_enabled", "night_cool_start", "night_cool_end", "night_cool_forecast_c", "night_cool_today_c",
        "night_cool_season_c", "night_cool_delta_k", "night_cool_min_indoor_c", "night_cool_level",
    )
    ADVANCED_LABELS = {
        "house_area_m2": "Boligareal", "ceiling_height_m": "Loftshøjde",
        "house_bathrooms": "Badeværelser", "house_utility_rooms": "Bryggers/toiletter",
        "airflow_max_m3h": "Maks. luftmængde", "sizing_reduced_percent": "Reduceret minimum",
        "afterheat_room_target": "Ønsket rumtemperatur", "afterheat_room_gain": "Forstærkning",
        "afterheat_room_min": "Laveste indblæsning", "afterheat_room_max": "Højeste indblæsning",
        "afterheat_room_step_minutes": "Minutter pr. trin",
        "afterheat_room_integral_minutes": "Integraltid (rum)", "afterheat_room_max_step": "Største trin",
        "afterheat_room_band_k": "Dødbånd om rummålet", "afterheat_room_offset_k": "Kanaltab / forskydning",
        "night_cool_forecast_c": "Natkøling: varm dag i morgen fra", "night_cool_today_c": "Natkøling: varm dag i dag fra",
        "night_cool_season_c": "Natkøling: kølesæson over", "night_cool_delta_k": "Natkøling: ude køligere end inde med",
        "night_cool_min_indoor_c": "Natkøling: inde mindst", "night_cool_level": "Natkøling: trin",
        "fireplace_auto_on_temp": "Pejs start", "fireplace_auto_off_temp": "Pejs stop",
        "fireplace_afterrun_minutes": "Efterløb", "fireplace_max_hours": "Maks. varighed",
        "fireplace_cold_guard_enabled": "Pejs stopper ved kold indblæsning",
        "humidity_rise_min_rh": "Fugtstigning tæller fra",
        "humidity_margin_gm3": "Fugtmargin", "dry_rh_limit": "Tør luft-grænse",
        "dry_max_level": "Maks. trin ved tør luft", "dry_hold_minutes": "Tør luft: mindste tid",
        "dry_release_steps_enabled": "Tør luft: slip i trin", "dry_release_step_ppm": "Tør luft: CO2 pr. trin",
        "dry_dynamic_enabled": "Dynamisk fugtbeskyttelse",
        "dry_tier1_rh": "Meget tør luft under", "dry_tier1_level": "Trin ved meget tør luft",
        "dry_tier2_rh": "Tør luft under", "dry_tier2_level": "Trin ved tør luft",
        "dry_dynamic_rh_hysteresis": "Fugt-hysterese", "dry_ah_margin_gm3": "Udeluften tørrere med mindst",
        "dry_ah_hysteresis_gm3": "Hysterese for absolut fugt", "dry_min_level": "Mindste ventilation",
        "dry_co2_soft_enabled": "Tør luft: CO2 løfter kun ét trin", "dry_co2_release_ppm": "Tør luft: CO2 slipper over",
        "vacation_afterheat_enabled": "Ferie: koldere indblæsning", "vacation_afterheat_drop_k": "Ferie: sænk indblæsningen med",
        "cooling_afterheat_hold_enabled": "Frikøling: ingen eftervarme",
        "balance_extract_excess_percent": "Udsugning over indblæsning", "balance_duct_ratio": "Kanalforhold",
        "duct_rooms_offset_k": "Korrektion udsugningsrum", "duct_t3_offset_k": "Korrektion T3",
        "t2_offset_k": "Korrektion T2",
        "pm25_setpoint": "PM2.5-grænse", "pm25_step": "PM2.5 pr. trin", "pm25_hysteresis": "PM2.5-hysterese",
        "pm25_max_level": "Maks. trin ved PM2.5",
        "bathroom_rise_percent": "Fugtstigning ved bad", "bathroom_rise_minutes": "Tid for fugtstigning",
        "bathroom_max_minutes": "Maks. udtørringstid",
        "winter_outdoor_max": "Vinter: koldt ude under", "winter_outdoor_hysteresis": "Vinter: hysterese ude",
        "winter_rh_limit": "Vinter: tør luft under", "winter_rh_hysteresis": "Vinter: hysterese fugt",
        "winter_level": "Vinter: grundtrin", "winter_hold_minutes": "Vinter: mindste tid",
        "frost_lock_outdoor_c": "Frostlås: ude eller loft ved højst",
        "frost_lock_coil_c": "Frostlås: frostføler ved højst",
        "frost_lock_min_setpoint_c": "Frostlås: mindste setpunkt",
        "afterheat_pi_gain": "Eftervarme: forstærkning",
        "afterheat_pi_integral_minutes": "Eftervarme: integraltid",
        "afterheat_pi_band": "Eftervarme: komfortbånd",
        "afterheat_pi_min_dwell_ticks": "Eftervarme: mindste tid pr. trin",
        "afterheat_algorithm": "Eftervarme: styring",
    }

    def frost_lock_in_force(self) -> bool:
        """The frost lock holds the afterheat: enabled, a water coil and frost latched by the engine."""
        d = self.data
        return (d.get("frost_lock_enabled") is True and d.get("afterheat_coil") == "water"
                and d.get("frost_lock_latched") is True)

    def frost_lock_floor(self) -> float:
        return float(self.data.get("frost_lock_min_setpoint_c", self.DEFAULTS["frost_lock_min_setpoint_c"]))

    def _check_frost_lock(self, patch: dict[str, object], enabled_before: object, setpoint_before: float) -> None:
        """Refuse what would switch the afterheat off while it freezes.

        Forms resend every field, so a value equal to the stored one is fine.
        The stored choices are never rewritten; the engine enforces the floor.
        """
        if not self.frost_lock_in_force():
            return
        if patch.get("afterheat_enabled") is False and enabled_before is not False:
            raise ControllerError(FROST_LOCK_OFF_ERROR)
        if "afterheat_setpoint" in patch:
            floor = self.frost_lock_floor()
            value = float(patch["afterheat_setpoint"])
            if value < floor and value < setpoint_before:
                raise ControllerError(FROST_LOCK_FLOOR_ERROR.format(floor=_danish_number(floor)))
        if "cool_boost_minutes" in patch and int(patch["cool_boost_minutes"]) > 0:
            raise ControllerError(FROST_LOCK_COOL_ERROR)

    def _configure_six_steps(self, patch: dict[str, object]) -> None:
        """Six steps: extract and supply gear set per step, as before."""
        incoming = patch["profiles"]
        if not isinstance(incoming, dict):
            raise ControllerError("profiles skal være et objekt")
        balance_was_on = self.data.get("balance_enabled") is True
        balance_on = patch.get("balance_enabled", balance_was_on) is True
        running = self.data["profiles"]
        table = _copy_profiles(self.data["six_step_profiles"])
        for raw_level, values in incoming.items():
            level = int(raw_level)
            if not 1 <= level <= 6 or not isinstance(values, dict):
                raise ControllerError("Kun trin 1..6 understøttes")
            extract = int(values.get("extract", table[level]["extract"]))
            supply = int(values.get("supply", running[level]["supply"]))
            if balance_on:
                # The balance solves supply from extract; resending the
                # current value (a form saving every field) is fine.
                if balance_was_on and supply != running[level]["supply"]:
                    raise ControllerError("Indblæsningen styres af luftbalancen. Ændr udsugningen, eller slå luftbalancen fra for at sætte indblæsningen selv")
                table[level]["extract"] = extract
            else:
                table[level].update(extract=extract, supply=supply)
            if "name" in values:
                name = str(values["name"]).strip()
                if not name or len(name) > 24:
                    raise ControllerError("Profilnavn skal være 1..24 tegn")
                table[level]["name"] = name
        if balance_on:
            for level in range(2, 7):
                if table[level]["extract"] <= table[level - 1]["extract"]:
                    raise ControllerError("Udsugningen skal stige fra trin til trin")
        else:
            validate_ladder(table)
        self.data["six_step_profiles"] = table

    def _configure_advanced(self, patch: dict[str, object]) -> None:
        for key in self.BOOL_KEYS:
            if key in patch:
                if not isinstance(patch[key], bool):
                    raise ControllerError(f"{key} skal være boolean")
                self.data[key] = patch[key]
        if "balance_ratio_mode" in patch:
            if patch["balance_ratio_mode"] not in BALANCE_RATIO_MODES:
                raise ControllerError("balance_ratio_mode skal være auto eller fixed")
            self.data["balance_ratio_mode"] = patch["balance_ratio_mode"]
        if "afterheat_control_mode" in patch:
            if patch["afterheat_control_mode"] not in AFTERHEAT_CONTROL_MODES:
                raise ControllerError("afterheat_control_mode skal være native eller pi")
            self.data["afterheat_control_mode"] = patch["afterheat_control_mode"]
        if "afterheat_algorithm" in patch:
            if patch["afterheat_algorithm"] not in AFTERHEAT_ALGORITHMS:
                raise ControllerError("afterheat_algorithm skal være en af " + ", ".join(AFTERHEAT_ALGORITHMS))
            self.data["afterheat_algorithm"] = patch["afterheat_algorithm"]
        if "winter_rh_basis" in patch:
            if patch["winter_rh_basis"] not in WINTER_RH_BASES:
                raise ControllerError("winter_rh_basis skal være average eller lowest")
            self.data["winter_rh_basis"] = patch["winter_rh_basis"]
        if patch.get("balance_learning_reset") is True:
            # Start the heat-balance learning over; the standby draw is kept.
            learned = self.data.get("balance_learned") or {}
            self.data["balance_learned"] = {"windows": [], "idle_power_w": learned.get("idle_power_w")}
        elif "balance_learning_reset" in patch and patch["balance_learning_reset"] is not False:
            raise ControllerError("balance_learning_reset skal være true")
        for key, low, high in self.ADVANCED_FLOATS:
            if key in patch:
                try:
                    value = float(patch[key])
                except (TypeError, ValueError) as error:
                    raise ControllerError(f"{self.ADVANCED_LABELS[key]} skal være et tal") from error
                if not low <= value <= high:
                    raise ControllerError(f"{self.ADVANCED_LABELS[key]} skal være {low:g}..{high:g}")
                self.data[key] = value
        for key, low, high in self.ADVANCED_INTS:
            high = self.max_level() if high is None else high  # step choices
            if key in patch:
                try:
                    value = _whole_number(patch[key])  # 2.5 is refused, not cut to 2
                except (TypeError, ValueError) as error:
                    raise ControllerError(f"{self.ADVANCED_LABELS[key]} skal være et heltal") from error
                if not low <= value <= high:
                    raise ControllerError(f"{self.ADVANCED_LABELS[key]} skal være {low}..{high}")
                self.data[key] = value
        if int(self.data["afterheat_room_min"]) > int(self.data["afterheat_room_max"]):
            raise ControllerError("Laveste indblæsning skal være under højeste")
        if float(self.data["dry_tier1_rh"]) >= float(self.data["dry_tier2_rh"]):
            raise ControllerError("Grænsen for meget tør luft skal være under grænsen for tør luft")
        if int(self.data["dry_tier1_level"]) > int(self.data["dry_tier2_level"]):
            raise ControllerError("Trinnet ved meget tør luft må ikke være over trinnet ved tør luft")
        for key, label, _default in self.TIME_KEYS:
            if key in patch:
                self.data[key] = _validate_time(patch[key], label)
        if float(self.data["fireplace_auto_off_temp"]) >= float(self.data["fireplace_auto_on_temp"]):
            raise ControllerError("Pejs stop skal være lavere end pejs start")
        for key, allow_fixed in (
            ("afterheat_room_source", True), ("fireplace_auto_source", False),
            ("outdoor_humidity_source", False), ("duct_extract_source", False),
        ):
            if key in patch:
                try:
                    self.data[key] = valid_source(patch[key], allow_fixed=allow_fixed,
                                                   allow_weather=key == "outdoor_humidity_source")
                except ValueError as error:
                    raise ControllerError(f"{key}: ugyldig målekilde") from error
        if "airflow_measured" in patch:
            self.data["airflow_measured"] = self._stamp_airflow(self._clean_airflow(patch["airflow_measured"]))
        if "onewire_roles" in patch:
            try:
                self.data["onewire_roles"] = clean_roles(patch["onewire_roles"])
            except ValueError as error:
                raise ControllerError(str(error)) from error
        if "pm25_ignored_rooms" in patch:
            self.data["pm25_ignored_rooms"] = self._clean_room_names(patch["pm25_ignored_rooms"])
        if "bathroom_rooms" in patch:
            self.data["bathroom_rooms"] = self._clean_bathroom_rooms(patch["bathroom_rooms"])
        if "room_setup" in patch:
            try:
                self.data["room_setup"] = clean_rooms(patch["room_setup"], max_level=self.max_level())
            except RoomSetupError as error:
                raise ControllerError(str(error)) from error
            # Saved in the WebUI: the old HA rooms are never moved in again.
            self.data["room_setup_migrated"] = True

    def _clean_bathroom_rooms(self, value: object) -> dict[str, dict[str, object]]:
        """Per-room bathroom settings; empty entries are dropped. Blank = use the common value."""
        if value in (None, ""):
            return {}
        if not isinstance(value, dict) or len(value) > 32:
            raise ControllerError("Badeværelsesindstillinger skal være et objekt med højst 32 rum")
        top = self.max_level()
        cleaned: dict[str, dict[str, object]] = {}
        for raw_name, raw in value.items():
            name = str(raw_name).strip()[:64]
            if not name or not isinstance(raw, dict):
                continue
            room_type = raw.get("type", "auto")
            if room_type not in ("auto", "bathroom", "normal"):
                raise ControllerError(f"{name}: rumtypen skal være auto, badeværelse eller normal")
            entry: dict[str, object] = {"type": room_type}
            for key, low, high, label, kind in (
                ("rh_setpoint", 35.0, 90.0, "fugtgrænse", float),
                ("rh_hysteresis", 1.0, 20.0, "hysterese", float),
                ("max_level", 1, top, "maks. trin", int),
            ):
                if raw.get(key) in (None, ""):
                    continue
                try:
                    number = kind(raw[key])
                except (TypeError, ValueError) as error:
                    raise ControllerError(f"{name}: {label} skal være et tal") from error
                if not low <= number <= high:
                    raise ControllerError(f"{name}: {label} skal være {low:g}..{high:g}")
                entry[key] = number
            if len(entry) > 1 or room_type != "auto":
                cleaned[name] = entry
        return cleaned

    @staticmethod
    def _clean_room_names(value: object) -> list[str]:
        if value in (None, ""):
            return []
        if not isinstance(value, list) or len(value) > 32:
            raise ControllerError("Rumlisten skal være en liste med højst 32 rum")
        return sorted({str(name).strip()[:64] for name in value if str(name).strip()})

    def heartbeat(self, demand: str = "normal", *, requested_level: int | None = None, valid_for_s: int | None = None, reason: str | None = None, bathroom_drying: bool = False, humidity_demand: bool = False) -> dict[str, object]:
        if demand not in VALID_DEMANDS:
            raise ControllerError("Ugyldigt HA-demand")
        # Older integrations still ask for 5 or 6: that is the maximum.
        if requested_level is not None and not 1 <= int(requested_level) <= 6:
            raise ControllerError(f"HA requested level skal være 1..{self.max_level()}")
        if requested_level is not None:
            requested_level = min(self.max_level(), int(requested_level))
        if valid_for_s is not None and not 30 <= int(valid_for_s) <= 900:
            raise ControllerError("HA lease skal være 30..900 sekunder")
        with self.lock:
            self.data["ha_last_seen"] = time.time()
            self.data["ha_demand"] = demand
            if requested_level is not None:
                self.data["ha_requested_level"] = int(requested_level)
            if valid_for_s is not None:
                self.data["ha_valid_for_seconds"] = int(valid_for_s)
            if reason is not None:
                self.data["ha_reason"] = str(reason)[:256]
            self.data["ha_bathroom_drying"] = bool(bathroom_drying)
            # Room humidity (not CO2) asks for more than the base level.
            self.data["ha_humidity_demand"] = bool(humidity_demand)
            return self.snapshot()

    def _expire_fireplace(self, now: float | None = None) -> None:
        now = now or time.time()
        until = self.data.get("fireplace_until")
        if self.data.get("fireplace") and (until is None or float(until) <= now):
            self.data["fireplace"] = False
            self.data["fireplace_until"] = None
            self.data["fireplace_duration_minutes"] = 0
            self.data["updated_at"] = now
            self.save()

    def _expire_vacation(self, now: float | None = None) -> None:
        now = now or time.time()
        if not self.data.get("vacation_enabled") or not self.data.get("vacation_until"):
            return
        try:
            until = _parse_until(self.data.get("vacation_until"), "Ferie slut")
        except ControllerError:
            until = None
        if until is not None and until <= now:
            self.data["vacation_enabled"] = False
            self.data["vacation_until"] = None
            self.data["vacation_from"] = None
            self.data["updated_at"] = now
            self.save()

    def _expire_quick_boost(self, now: float | None = None) -> None:
        now = now or time.time()
        until = self.data.get("quick_boost_until")
        if until is None:
            return
        try:
            active_until = float(until)
        except (TypeError, ValueError):
            active_until = 0.0
        if active_until <= now:
            self.data["quick_boost_until"] = None
            self.data["quick_boost_minutes"] = 0
            self.data["updated_at"] = now
            self.save()

    def _expire_cool_boost(self, now: float | None = None) -> None:
        now = now or time.time()
        until = self.data.get("cool_boost_until")
        if until is None:
            return
        try:
            active_until = float(until)
        except (TypeError, ValueError):
            active_until = 0.0
        if active_until <= now:
            self.data["cool_boost_until"] = None
            self.data["cool_boost_minutes"] = 0
            self.data["updated_at"] = now
            self.save()

    def _expire_max_level(self, now: float | None = None) -> None:
        """Step 4 chosen by hand returns to step 3, like the HCP4 panel."""
        now = now or time.time()
        until = self.data.get("max_level_until")
        if until is None:
            return
        try:
            active_until = float(until)
        except (TypeError, ValueError):
            active_until = 0.0
        if active_until <= now:
            if self.top_hold_active():
                self.data["manual_level"] = self.top_return_level()
            self.data["max_level_until"] = None
            self.data["updated_at"] = now
            self.save()

    def _expire_bonfire(self, now: float | None = None) -> None:
        now = now or time.time()
        until = self.data.get("bonfire_until")
        if until is None:
            return
        try:
            active_until = float(until)
        except (TypeError, ValueError):
            active_until = 0.0
        if active_until <= now:
            self.data["bonfire_until"] = None
            self.data["bonfire_minutes"] = 0
            self.data["updated_at"] = now
            self.save()

    def _expire_standby(self, now: float | None = None) -> None:
        now = now or time.time()
        until = self.data.get("standby_until")
        if not self.data.get("standby") or until is None:
            return
        try:
            active_until = float(until)
        except (TypeError, ValueError):
            active_until = 0.0
        if active_until <= now:
            self.data["standby"], self.data["standby_until"], self.data["standby_minutes"] = False, None, 0
            self.data["updated_at"] = now
            self.save()

    def snapshot(self) -> dict[str, object]:
        with self.lock:
            now = time.time()
            self._expire_standby(now)
            self._expire_fireplace(now)
            self._expire_vacation(now)
            self._expire_quick_boost(now)
            self._expire_cool_boost(now)
            self._expire_bonfire(now)
            self._expire_max_level(now)
            result = dict(self.data)
            # The room list is read from GET /api/controller/rooms; the state polled every few seconds keeps a count.
            result["room_setup_count"] = len(result.pop("room_setup", None) or [])
            result["profiles"] = _copy_profiles(self.data["profiles"])
            result["schedule"] = _copy_schedule(self.data["schedule"])
            seen = result.get("ha_last_seen")
            lease = min(int(result["ha_timeout_seconds"]), int(result.get("ha_valid_for_seconds", result["ha_timeout_seconds"])))
            result["ha_online"] = bool(seen and now - float(seen) <= lease + HA_LEASE_GRACE_SECONDS)
            result["ha_age_seconds"] = round(now - float(seen), 1) if seen else None
            until = result.get("fireplace_until")
            result["fireplace_remaining_seconds"] = max(0, int(float(until) - now)) if until else 0
            boost_until = result.get("quick_boost_until")
            result["quick_boost_remaining_seconds"] = max(0, int(float(boost_until) - now)) if boost_until else 0
            cool_until = result.get("cool_boost_until")
            result["cool_boost_remaining_seconds"] = max(0, int(float(cool_until) - now)) if cool_until else 0
            result["cool_boost_active"] = bool(result["cool_boost_remaining_seconds"])
            bonfire_until = result.get("bonfire_until")
            result["bonfire_remaining_seconds"] = max(0, int(float(bonfire_until) - now)) if bonfire_until else 0
            result["bonfire_active"] = bool(result["bonfire_remaining_seconds"])
            max_until = result.get("max_level_until")
            result["max_level_remaining_seconds"] = max(0, int(float(max_until) - now)) if max_until else 0
            result["fan_levels"] = list(range(1, self.max_level() + 1))
            result["max_level"] = self.max_level()
            result["six_step_profiles"] = _copy_profiles(self.data["six_step_profiles"])
            result["fan_steps"] = {
                "settings": dict(self.data["fan_settings"]),
                "ladder": _copy_profiles(fan_steps.ladder(self.data["fan_settings"])),
                "nominal_range": list(fan_steps.NOMINAL_RANGE),
                "offset_range": list(fan_steps.OFFSET_RANGE),
                "offset_default": fan_steps.OFFSET_DEFAULT,
                "max_level_hours": MAX_LEVEL_HOURS,
            }
            standby_until = result.get("standby_until")
            result["standby_active"] = result.get("standby") is True
            result["standby_remaining_seconds"] = max(0, int(float(standby_until) - now)) if standby_until else None
            vacation_until = _parse_until(result.get("vacation_until"), "Ferie slut") if result.get("vacation_until") else None
            result["vacation_remaining_seconds"] = max(0, int(vacation_until - now)) if vacation_until else None
            vacation_from = _parse_until(result.get("vacation_from"), "Ferie start") if result.get("vacation_from") else None
            result["vacation_pending"] = bool(result.get("vacation_enabled")) and not self.vacation_running(now)
            result["vacation_starts_in_seconds"] = max(0, int(vacation_from - now)) if vacation_from and vacation_from > now else None
            periods = self.schedule_periods()
            local_now = datetime.fromtimestamp(now).astimezone()
            planned = week_schedule.effect(periods, local_now)
            result["schedule_periods"] = periods
            result["schedule_source"] = "periods" if self.data.get("schedule_periods") is not None else "legacy"
            result["schedule_now"] = None if planned is None else {
                **planned["period"], "set_level": planned["set_level"], "min_level": planned["min_level"]}
            result["schedule_next_change"] = week_schedule.next_change(periods, local_now)
            level = result.get("effective_level")
            result["effective_profile"] = result["profiles"].get(int(level)) if level else None
            if result["bonfire_active"] and not result.get("fireplace"):
                result["effective_profile"] = dict(BONFIRE_PROFILE)
            if result["standby_active"]:
                result["effective_profile"] = dict(STANDBY_PROFILE)
            result["airflow_plan"] = self.airflow_plan()
            result["effective_normal_level"] = self.normal_level()
            result["balance"] = self.balance_status()
            # The windows stay on the Pi; the summary is in balance.learned.
            result["balance_learned"] = {key: value for key, value in (result.get("balance_learned") or {}).items() if key != "windows"} or None
            return result


_MISSING_MEASUREMENT = object()


class ControllerEngine:
    """Decision engine, automation overlays, write deduplication and HA fallback."""

    def __init__(self, config: ControllerState, hardware: HardwareAdapter | None = None) -> None:
        self.config = config
        self.hardware = hardware or HardwareAdapter()
        self.lock = threading.RLock()
        self.measurements: dict[str, float | bool | None] = {"rh": None, "co2": None, "outdoor": None, "room": None}
        # Values from other sources (T3, HA rooms). Replaced as a whole on every
        # refresh so a vanished HA value becomes None instead of sticking.
        self.external: dict[str, float | None] = {}
        # Supply target from the rooms (outer loop, see room_cascade).
        self.room_cascade = RoomCascade()
        self.afterheat_room_output: float | None = None
        self.afterheat_room_state = "disabled"
        self.afterheat_room_saved_at = 0.0
        # Night cooling from the forecast and the outdoor history it uses (night_cooling.py).
        self.night_cooling = NightCooling()
        self.outdoor_history = OutdoorHistory(config.data.get("outdoor_daily_means"))
        # The last afterheat control snapshot (saturation for the room loop).
        self.last_pilot: dict[str, object] = {}
        # Pi-side afterheat level control (afterheat_pilot) and what it is fed with.
        # What the selectable afterheat algorithms learn is kept next to the settings.
        config_path = getattr(config, "path", None)
        self.afterheat_pilot = AlgorithmPilot(
            store_path=Path(config_path).with_name("afterheat_algorithms.json") if config_path else None)
        self.afterheat_inputs: dict[str, object] = {}
        self.last_afterheat: dict[str, object] = {}
        self.current_auto_level: int | None = None
        self.last_level_change = 0.0
        self.boost_until = 0.0
        # Since when the wanted auto level has been below / above the current one
        # (a step down needs the whole downshift delay of lower demand), and who
        # set the current level: "ha" (Smart Auto) or "local" (the unit's sensors).
        self.lower_since: float | None = None
        self.higher_since: float | None = None
        self.auto_level_owner: str | None = None
        self.cooling_active = False
        self.cooling_qualifying_since: float | None = None
        self.cooling_last_on_at: float | None = None
        self.cooling_last_off_at: float | None = None
        self.cooling_reason = "disabled"
        # Winter dry-air mode (see _update_winter).
        self.winter_active = False
        self.winter_changed_at: float | None = None
        self.winter_pause_until = 0.0
        self.winter_pause_reason: str | None = None
        self.winter_state = "disabled"
        # Dry-air limit (see _dry_air): a held decision, so it cannot flap.
        self.dry_active = False
        self.dry_changed_at: float | None = None
        self.dry_state = "disabled"
        # After high CO2 ended the limit it waits for CO2 <= setpoint - hysteresis.
        self.dry_co2_stopped = False
        # Dry-air release in steps (see _dry_release_cap): the rooms are still dry
        # with no humidity demand, the highest CO2 then, and the last step cap.
        self.dry_rooms_dry = False
        self.dry_release_co2: float | None = None
        self.dry_release_cap: int | None = None
        # Dynamic dry-air protection (see _dry_dynamic): the held tier (0 none,
        # 1 very dry, 2 dry), whether the outdoor air dries (absolute humidity,
        # with hysteresis), the level cap, the house floor under it and why.
        self.dry_tier = 0
        self.dry_ah_open = False
        self.dry_cap: int | None = None
        self.dry_tier_level: int | None = None
        self.dry_floor: int | None = None
        self.dry_reason: str | None = None
        self.dry_release_base: int | None = None
        self.dry_air_values: dict[str, float | None] = {}
        # dry_co2_soft_enabled: CO2 over its setpoint lifts the tier cap one step (latched with co2_hysteresis).
        self.dry_co2_lift = False
        # The level the automatic control wanted before the dry-air cap (None: no cap now).
        self.dry_uncapped_level: int | None = None
        # Automatic level changes in the last hour (flap counter, see _count_level_change).
        self.level_change_times: deque[float] = deque()
        self._counted_level: int | None = None
        self._counted_context: tuple | None = None
        self._schedule_key: tuple | None = None
        # Frost lock (see _update_frost_lock); the latch itself is saved in the config.
        self.frost_lock_active = False
        self.cool_stopped_by_frost = False
        self.frost_lock_reason: str | None = None
        self.frost_release_since: float | None = None
        self.frost_cold_since: float | None = None
        self.frost_engage_seconds = FROST_LOCK_ENGAGE_SECONDS
        self.last_applied: dict[str, object] = {}
        self.last_applied_at: dict[str, float] = {}
        self.last_write_at: float | None = None
        self.last_error: str | None = None
        self.write_failures = 0
        self.started_at = time.time()
        self.retry_limit = 3
        self.retry_base_seconds = 5.0
        self._failure_values: dict[str, object] = {}
        self._failure_counts: dict[str, int] = {}
        self._retry_at: dict[str, float] = {}
        self.command_errors: dict[str, str] = {}

    def update_measurements(self, *, rh=_MISSING_MEASUREMENT, co2=_MISSING_MEASUREMENT,
                            outdoor=_MISSING_MEASUREMENT, room=_MISSING_MEASUREMENT) -> None:
        with self.lock:
            for key, value in (("rh", rh), ("co2", co2), ("outdoor", outdoor), ("room", room)):
                if value is _MISSING_MEASUREMENT:
                    continue
                try:
                    number = float(value) if not isinstance(value, bool) else float("nan")
                    self.measurements[key] = number if math.isfinite(number) else None
                except (TypeError, ValueError):
                    self.measurements[key] = None

    def set_external(self, values: dict[str, float | None]) -> None:
        with self.lock:
            self.external = dict(values)

    def set_afterheat_inputs(self, values: dict[str, object]) -> None:
        """Live HAC1 values for the Pi afterheat control (replaced as a whole).

        Keys: level, level_at, level_changed_at, t2ah, t2ah_at, t2ah_raw,
        fan_supply, water_flow (wall-clock times) and master (bool).
        """
        with self.lock:
            self.afterheat_inputs = dict(values)

    def outdoor_absolute_humidity(self) -> float | None:
        outdoor = self.measurements.get("outdoor")
        return absolute_humidity(
            outdoor if isinstance(outdoor, (int, float)) else None,
            self.external.get("outdoor_rh"),
        )

    def indoor_absolute_humidity(self) -> float | None:
        """Absolute humidity indoors: the fresh HA rooms (average RH at the room
        temperature), else the unit's extract RH at the extract temperature."""
        rooms = self.external.get("indoor_rh_average")
        if _is_number(rooms):
            return self._room_absolute_humidity(float(rooms))
        rh = self.measurements.get("rh")
        temperature = self.external.get("extract_temp")
        if not _is_number(temperature):
            room = self.measurements.get("room")
            temperature = room if _is_number(room) else None
        return absolute_humidity(temperature, rh if _is_number(rh) else None)

    def humidity_dries(self, indoor_ah: float | None) -> bool:
        """False only when we know outdoor air would not dry this air."""
        if not self.config.data.get("humidity_smart_enabled"):
            return True
        outdoor_ah = self.outdoor_absolute_humidity()
        if indoor_ah is None or outdoor_ah is None:
            return True
        return indoor_ah - outdoor_ah >= float(self.config.data["humidity_margin_gm3"])

    def _local_auto_level(self, now: float) -> tuple[int, str]:
        d = self.config.data
        normal = self.config.normal_level()
        wanted = normal
        reasons: list[str] = []
        rh = self.measurements.get("rh")
        co2 = self.measurements.get("co2")
        if isinstance(rh, (int, float)) and not self.humidity_dries(self.indoor_absolute_humidity()):
            if rh > float(d["rh_setpoint"]):
                reasons.append(f"RH {rh:.1f}% ignoreret: udeluften tørrer ikke")
            rh = None
        if isinstance(rh, (int, float)):
            delta = rh - float(d["rh_setpoint"])
            if delta > 0:
                wanted = max(wanted, normal + max(1, math.ceil(delta / max(1.0, float(d["auto_step_rh"])))))
                reasons.append(f"RH {rh:.1f}% > {float(d['rh_setpoint']):.1f}%")
        if isinstance(co2, (int, float)):
            delta = co2 - int(d["co2_setpoint"])
            if delta > 0:
                wanted = max(wanted, normal + max(1, math.ceil(delta / max(1, int(d["auto_step_co2"])))))
                reasons.append(f"CO2 {co2:.0f} ppm > {int(d['co2_setpoint'])} ppm")
        air_cap = max(normal, int(d["air_quality_max_level"]))
        wanted = min(int(d["local_max_level"]), air_cap, max(int(d["local_min_level"]), wanted))
        rh_clear = not isinstance(rh, (int, float)) or rh <= float(d["rh_setpoint"]) - float(d["rh_hysteresis"])
        co2_clear = not isinstance(co2, (int, float)) or co2 <= int(d["co2_setpoint"]) - int(d["co2_hysteresis"])
        # The hysteresis hold is for a level the unit's own CO2/RH raised. A level
        # inherited from Smart Auto (HA went away) steps down after the normal delay.
        inherited = self.auto_level_owner == "ha"
        current, held = self._stabilize_auto_level(
            wanted, now, allow_downshift=(rh_clear and co2_clear) or inherited, owner="local")
        if current > air_cap:
            current = air_cap
            self.current_auto_level = current
        if held:
            reasons.append(held)
        return current, ", ".join(reasons) if reasons else "RH/CO2 normal"

    def _stabilize_auto_level(self, wanted: int, now: float, *, allow_downshift: bool = True,
                              owner: str = "ha", urgent: bool = False) -> tuple[int, str | None]:
        """Follow the wanted level calmly.

        Up: at once, or after upshift_delay_seconds of steady demand for a single
        step (two steps or more, and ``urgent`` such as bathroom drying, go at once).
        Down: only when the wanted level has stayed lower for the whole downshift
        delay and the delay since the last change has passed, so one low sample
        right after a long steady spell does not step down.
        """
        d = self.config.data
        wanted = min(6, max(1, int(wanted)))
        if self.current_auto_level is None:
            # Nothing to hold yet (start-up, or after a gap in a replay): take it as it is.
            self.lower_since = self.higher_since = None
            self.auto_level_owner = owner
            self.current_auto_level = wanted
            if wanted != self.config.normal_level():
                self.last_level_change = now
            if wanted >= self.config.max_level():
                self.boost_until = now + int(d["boost_hold_seconds"])
            return wanted, None
        current = self.current_auto_level
        held = None
        if wanted > current:
            self.lower_since = None
            if self.higher_since is None:
                self.higher_since = now
            delay = int(d.get("upshift_delay_seconds") or 0)
            if urgent or delay <= 0 or wanted - current >= 2 or now - self.higher_since >= delay:
                current = wanted
                self.last_level_change = now
                self.higher_since = None
                self.auto_level_owner = owner
                if current >= self.config.max_level():
                    self.boost_until = now + int(d["boost_hold_seconds"])
            else:
                held = "Upshift delay"
        elif wanted < current:
            self.higher_since = None
            if self.lower_since is None:
                self.lower_since = now
            delay = int(d["downshift_delay_seconds"])
            if now < self.boost_until:
                held = "Boost hold"
            elif not allow_downshift:
                held = "Hysteresis"
            elif now - self.last_level_change >= delay and now - self.lower_since >= delay:
                current = wanted
                self.last_level_change = now
                self.lower_since = None
                self.auto_level_owner = owner
            else:
                held = "Downshift delay"
        else:
            # The level is what this caller wants too: it owns it now.
            self.lower_since = self.higher_since = None
            self.auto_level_owner = owner
        self.current_auto_level = current
        return current, held

    def _stop_cooling(self, now_ts: float, reason: str) -> None:
        if self.cooling_active:
            self.cooling_last_off_at = now_ts
        self.cooling_active = False
        self.cooling_qualifying_since = None
        self.cooling_reason = reason

    def _automation_overlay(self, level: int, source: str, reason: str, now_ts: float) -> tuple[int, str, str, str, dict[str, bool]]:
        d = self.config.data
        now = datetime.fromtimestamp(now_ts).astimezone()
        flags = {"schedule_active": False, "night_active": False, "vacation_active": False, "quick_boost_active": False, "cooling_active": False, "cool_boost_active": False,
                 "night_cool_active": False}
        effective_bypass = "on" if d["bypass"] == "on" else "off"
        self._schedule_key = None

        if self.config.vacation_running(now_ts):
            flags["vacation_active"] = True
            level = int(d["vacation_level"])
            source = "vacation"
            reason = f"Ferie mode · trin {level}"
            self._stop_cooling(now_ts, "vacation")
        elif d["mode"] != "manual":
            if d.get("schedule_enabled"):
                planned = week_schedule.effect(self.config.schedule_periods(), now)
                if planned:
                    flags["schedule_active"] = True
                    period = planned["period"]
                    self._schedule_key = (period.get("start"), period.get("end"), period.get("label"),
                                          planned.get("set_level"), planned.get("min_level"))
                    name = period["label"] or f"{period['start']}–{period['end']}"
                    if planned["set_level"] is not None:
                        # The period replaces the base level; air-quality demand above
                        # the normal base level may still lift it.
                        wanted = max(int(d["local_min_level"]), int(planned["set_level"]))
                        level = max(level, wanted) if level > self.config.normal_level() else wanted
                    if planned["min_level"] is not None:
                        level = max(level, int(planned["min_level"]))
                    reason = f"{reason}; ugeplan {name} · trin {planned['set_level'] or planned['min_level']}"
            if d.get("night_enabled") and _time_window_active(now, str(d["night_start"]), str(d["night_end"])):
                flags["night_active"] = True
                rh, co2 = self.measurements.get("rh"), self.measurements.get("co2")
                local_urgent = (
                    isinstance(rh, (int, float)) and rh > float(d["rh_setpoint"]) + float(d["rh_hysteresis"]) or
                    isinstance(co2, (int, float)) and co2 > int(d["co2_setpoint"]) + int(d["co2_hysteresis"])
                )
                # Night mode is a final policy layer, not a competing writer. HA Smart
                # demand may lift the night level for air quality, but only to a
                # configurable ceiling. This prevents a humid bathroom from repeatedly
                # forcing full boost while night mode simultaneously tries to reduce it.
                # Only real demand counts: the base level itself (good air) gets the
                # night level, as the setting promises.
                ha_urgent = source == "ha_smart" and level > self.config.normal_level()
                if source == "ha_smart" and d.get("ha_bathroom_drying") and d.get("bathroom_night_drying", True):
                    # Drying a bathroom out after a shower is allowed at night too;
                    # it steps down by itself as humidity falls.
                    flags["night_active"] = True
                    reason = f"{reason}; nat: badeværelse tørres ud"
                elif local_urgent or ha_urgent:
                    night_cap = max(int(d["night_level"]), int(d["night_air_quality_max_level"]))
                    source = "night_air_quality"
                    if level > night_cap:
                        level = night_cap
                        reason = f"{reason}; nat: luftkvalitet begrænset til trin {night_cap}"
                    else:
                        reason = f"{reason}; nat: luften kræver trin {level}"
                else:
                    level = min(level, int(d["night_level"]))
                    source = "night"
                    reason = f"Natsænkning {d['night_start']}–{d['night_end']}"

            room = self.measurements.get("room")
            outdoor = self.measurements.get("outdoor")
            if d.get("cooling_enabled") and isinstance(room, (int, float)) and isinstance(outdoor, (int, float)):
                setpoint = float(d["cooling_room_setpoint"])
                hysteresis = float(d["cooling_hysteresis"])
                minimum = float(d["cooling_outdoor_min"])
                delta_min = float(d["cooling_min_delta"])
                delta = room - outdoor
                start_ok = room >= setpoint + hysteresis and outdoor >= minimum and delta >= delta_min
                keep_ok = room > setpoint - hysteresis and outdoor >= minimum and delta >= max(0.2, delta_min - hysteresis)
                min_on = int(d["cooling_min_on_seconds"])
                min_off = int(d["cooling_min_off_seconds"])
                start_delay = int(d["cooling_start_delay_seconds"])

                if self.cooling_active:
                    on_age = now_ts - (self.cooling_last_on_at or now_ts)
                    if keep_ok:
                        self.cooling_reason = "active"
                    elif on_age < min_on:
                        self.cooling_reason = "minimum_on_hold"
                    else:
                        if outdoor < minimum:
                            stop_reason = "outdoor_too_cold"
                        elif delta < max(0.2, delta_min - hysteresis):
                            stop_reason = "not_cooler_outside"
                        else:
                            stop_reason = "room_satisfied"
                        self._stop_cooling(now_ts, stop_reason)
                else:
                    if not start_ok:
                        self.cooling_qualifying_since = None
                        if outdoor < minimum:
                            self.cooling_reason = "outdoor_too_cold"
                        elif delta < delta_min:
                            self.cooling_reason = "not_cooler_outside"
                        elif room < setpoint + hysteresis:
                            self.cooling_reason = "room_below_start"
                        else:
                            self.cooling_reason = "standby"
                    elif self.cooling_last_off_at is not None and now_ts - self.cooling_last_off_at < min_off:
                        self.cooling_qualifying_since = None
                        self.cooling_reason = "minimum_off_hold"
                    else:
                        if self.cooling_qualifying_since is None:
                            self.cooling_qualifying_since = now_ts
                        if now_ts - self.cooling_qualifying_since >= start_delay:
                            self.cooling_active = True
                            self.cooling_last_on_at = now_ts
                            self.cooling_qualifying_since = None
                            self.cooling_reason = "opening"
                        else:
                            self.cooling_reason = "qualifying"

                if self.cooling_active:
                    flags["cooling_active"] = True
                    effective_bypass = "on"
                    level = max(level, int(d["cooling_level"]))
                    source = "free_cooling"
                    reason = f"Frikøling ({self.cooling_reason}): inde {room:.1f}°C / ude {outdoor:.1f}°C"
            else:
                if d.get("cooling_enabled"):
                    self._stop_cooling(now_ts, "sensor_missing")
                else:
                    self._stop_cooling(now_ts, "disabled")
        else:
            self._stop_cooling(now_ts, "manual_mode")

        night_cool = self.night_cooling.update(
            now=now_ts, local=now, settings=d, indoor=self._night_indoor(), outdoor=self._night_outdoor(),
            season_mean=self.outdoor_history.running_mean(), forecast_high=self.external.get("forecast_high_c"),
            today_high=self._today_high(), blocked=self._night_cool_blocker(d, now_ts, flags))
        if night_cool["night_cool_active"]:
            flags["night_cool_active"] = True
            effective_bypass = "on"
            wanted = int(d["night_cool_level"])
            if flags["night_active"]:
                # Never louder than the night allows for poor air.
                wanted = min(wanted, max(int(d["night_level"]), int(d["night_air_quality_max_level"])))
            if wanted >= level:
                level = wanted
                source = "night_cooling"
                reason = f"{night_cool['night_cool_reason']} · bypass åben · trin {wanted}"
            else:
                reason = f"{reason}; {night_cool['night_cool_reason']} · bypass åben"

        flags["dry_protection_active"] = False
        self.dry_uncapped_level = None
        dry = self._dry_air(d, now_ts)
        if dry and d["mode"] != "manual" and not flags["cooling_active"] and not flags["night_cool_active"]:
            dynamic = self.dry_cap is not None
            limit = int(self.dry_cap) if dynamic else int(d["dry_max_level"])
            if level > limit:
                self.dry_uncapped_level = level
                level = limit
                flags["dry_protection_active"] = True
                source = "dry_protection"
                if dynamic:
                    reason = str(self.dry_reason)
                else:
                    rh = self.ha_indoor_rh()
                    rh_text = f"{rh:.0f}" if rh is not None else "?"
                    reason = f"Tør luft (rum {rh_text}% RH): begrænset til trin {limit}"
        flags["dry_release_active"] = False
        cap = self._dry_release_cap(d) if (
            d["mode"] != "manual" and not flags["cooling_active"] and not flags["night_cool_active"]) else None
        if cap is not None and level > cap:
            self.dry_uncapped_level = max(level, self.dry_uncapped_level or 0)
            level = cap
            flags["dry_release_active"] = True
            source = "dry_protection"
            reason = f"{reason}; tør luft slipper i trin: højst trin {cap} lige nu"

        flags["sizing_floor_active"] = False
        if d.get("sizing_enabled") and d["mode"] != "manual":
            floor = int(self.config.airflow_plan()["min_level"])
            if level < floor:
                level = floor
                flags["sizing_floor_active"] = True
                reason = f"{reason}; husets minimum trin {floor}"

        boost_until = d.get("quick_boost_until")
        if not d.get("fireplace") and boost_until and float(boost_until) > now_ts:
            flags["quick_boost_active"] = True
            level = max(level, int(d["quick_boost_level"]))
            source = "quick_boost"
            remaining = max(1, math.ceil((float(boost_until) - now_ts) / 60))
            reason = f"Quick Boost · trin {int(d['quick_boost_level'])} · ca. {remaining} min tilbage"

        cool_until = d.get("cool_boost_until")
        if not d.get("fireplace") and cool_until and float(cool_until) > now_ts:
            # Cool: open the bypass and run the top step, whatever else is going on.
            flags["cool_boost_active"] = True
            top = self.config.max_level()
            level = top
            effective_bypass = "on"
            source = "cool_boost"
            remaining = max(1, math.ceil((float(cool_until) - now_ts) / 60))
            reason = f"Køl · bypass åben · trin {top} · ca. {remaining} min tilbage"

        if d.get("fireplace"):
            effective_bypass = "off"
        # The Auto limits are for automatic control: a step chosen by hand is
        # never moved by them, and vacation chooses its own (lower) step.
        if (d["mode"] != "manual" or source != "manual") and source != "cool_boost":
            auto_floor = 1 if flags["vacation_active"] else int(d["local_min_level"])
            level = min(int(d["local_max_level"]), max(auto_floor, int(level)))
        standby_until = d.get("standby_until")
        flags["standby_active"] = d.get("standby") is True and (standby_until is None or float(standby_until) > now_ts)
        if flags["standby_active"]:
            flags["quick_boost_active"] = False
            flags["cool_boost_active"] = False
            flags["cooling_active"] = False
            self._stop_cooling(now_ts, "standby")
            left = f" · ca. {max(1, math.ceil((float(standby_until) - now_ts) / 60))} min tilbage" if standby_until else " · indtil det tændes"
            flags["bonfire_active"] = False
            return level, "standby", f"Anlæg slukket (standby){left}", "off", flags
        bonfire_until = d.get("bonfire_until")
        flags["bonfire_active"] = False
        if not d.get("fireplace") and bonfire_until and float(bonfire_until) > now_ts:
            # Smoke outside: the unit is switched off by the standby pattern in apply().
            flags["bonfire_active"] = True
            flags["quick_boost_active"] = False
            flags["cool_boost_active"] = False
            flags["cooling_active"] = False
            self._stop_cooling(now_ts, "bonfire")
            effective_bypass = "off"
            level = 1
            source = "bonfire"
            remaining = max(1, math.ceil((float(bonfire_until) - now_ts) / 60))
            reason = f"Bål i haven · anlæg slukket · ca. {remaining} min tilbage"
        return level, source, reason, effective_bypass, flags

    def _dry_dynamic_snapshot(self, d: dict) -> dict[str, object]:
        """The dynamic dry-air protection for the WebUI and Home Assistant (dry_* keys)."""
        values = self.dry_air_values
        dynamic = bool(d.get("dry_protection_enabled") and d.get("dry_dynamic_enabled"))
        diff = values.get("difference")
        return {
            "dry_dynamic_enabled": dynamic,
            "dry_tier": self.dry_tier if dynamic else None,
            "dry_tier_level": self.dry_tier_level,
            "dry_cap_level": self.dry_cap,
            "dry_floor_level": self.dry_floor,
            "dry_floor_limited": (self.dry_tier_level is not None and self.dry_floor is not None
                                  and self.dry_tier_level < self.dry_floor),
            "dry_reason": self.dry_reason,
            "dry_uncapped_level": self.dry_uncapped_level,
            "dry_indoor_temperature": _round(values.get("indoor_temperature"), 2),
            "dry_outdoor_temperature": _round(values.get("outdoor_temperature"), 2),
            "dry_outdoor_rh": _round(values.get("outdoor_rh"), 1),
            "dry_outdoor_source": self.external.get("dry_outdoor_source") if dynamic else None,
            "dry_indoor_ah": _round(values.get("indoor_ah")),
            "dry_outdoor_ah": _round(values.get("outdoor_ah")),
            "dry_ah_difference": _round(diff),
            "dry_ah_drying": self.dry_ah_open if dynamic and diff is not None else None,
            "dry_co2_lift": self.dry_co2_lift if dynamic else None,
        }

    def ha_indoor_rh(self) -> float | None:
        """RH of the Home Assistant rooms (winter_rh_basis); None while HA has no fresh rooms."""
        key = "indoor_rh_lowest" if self.config.data.get("winter_rh_basis") == "lowest" else "indoor_rh_average"
        value = self.external.get(key)
        return float(value) if _is_number(value) else None

    def _room_absolute_humidity(self, rh: float) -> float | None:
        """Absolute humidity of room air at this RH: the HA rooms' temperature first.

        The extract air (T3) is a few tenths to a degree colder than the rooms,
        which under-estimates the indoor absolute humidity, so it is only used
        when no room temperature is known.
        """
        for temperature in (self.external.get("indoor_temperature_average"),
                            self.external.get("afterheat_room_temperature"),
                            self.external.get("extract_temp"),
                            self.measurements.get("room")):
            if _is_number(temperature):
                return absolute_humidity(float(temperature), rh)
        return None

    def _humidity_demand(self, d: dict, now: float) -> str | None:
        """Humidity demand the dry-air limit must never block (None when there is none).

        Bathroom drying and room humidity from Home Assistant, and the local RH
        demand of the unit's own sensor (bathrooms are not in the HA room RH).
        """
        if self._ha_live(d, now):
            if d.get("ha_bathroom_drying"):
                return "bathroom_drying"
            if d.get("ha_humidity_demand"):
                return "ha_humidity"
        rh = self.measurements.get("rh")
        if _is_number(rh) and rh > float(d["rh_setpoint"]) and self.humidity_dries(self.indoor_absolute_humidity()):
            return "local_rh"
        return None

    def _dry_air(self, d: dict, now: float) -> bool:
        """Dry-air limit: the HA rooms are dry, the outdoor air dries and CO2 is fine.

        Uses only the Home Assistant rooms' RH: the unit's own extract RH reads
        13-17 % while the rooms read ~40 %, which held the limit on and let
        every CO2 wobble flip the level (626 changes on 2026-11-19). Without
        fresh HA rooms the limit is not applied at all. Start below
        dry_rh_limit with CO2 at or under its setpoint, stop at limit +
        winter_rh_hysteresis; each decision holds for dry_hold_minutes. CO2
        over its setpoint and any humidity demand (bathroom drying, HA room
        humidity, the local RH demand) end it at once. After a CO2 stop (or CO2
        over its setpoint while the limit is off) it only starts again with CO2
        at or under setpoint - hysteresis.
        """
        self.dry_rooms_dry, self.dry_release_co2 = False, None
        self.dry_cap = self.dry_tier_level = self.dry_floor = self.dry_reason = self.dry_release_base = None
        self.dry_air_values = {}
        dynamic = bool(d.get("dry_protection_enabled") and d.get("dry_dynamic_enabled"))
        if not dynamic:
            self.dry_tier, self.dry_ah_open, self.dry_co2_lift = 0, False, False
        if not d.get("dry_protection_enabled"):
            self.dry_active, self.dry_changed_at, self.dry_state = False, None, "disabled"
            self.dry_co2_stopped = False
            return False
        if dynamic:
            return self._dry_dynamic(d, now)
        rh = self.ha_indoor_rh()
        if rh is None:
            if self.dry_active:
                self.dry_changed_at = now
            self.dry_active, self.dry_state, self.dry_co2_stopped = False, "sensor_missing", False
            return False
        limit = float(d["dry_rh_limit"])
        hysteresis = float(d["winter_rh_hysteresis"])
        hold = int(d.get("dry_hold_minutes", 10)) * 60
        if rh >= limit + hysteresis:
            self.dry_co2_stopped = False  # no longer dry: the next start is a first start
        co2_limit = int(d["co2_setpoint"])
        co2_start_limit = co2_limit - int(d["co2_hysteresis"]) if self.dry_co2_stopped else co2_limit
        co2_values = [
            float(co2) for co2 in (self.measurements.get("co2"), self.external.get("max_room_co2"))
            if _is_number(co2)
        ]
        co2_high = any(co2 > co2_limit for co2 in co2_values)
        co2_clear = all(co2 <= co2_start_limit for co2 in co2_values)
        demand = self._humidity_demand(d, now)
        outdoor_ah, indoor_ah = self.outdoor_absolute_humidity(), self._room_absolute_humidity(rh)
        drying = outdoor_ah is None or indoor_ah is None or outdoor_ah < indoor_ah
        dry = rh < limit and drying
        self.dry_rooms_dry = dry and demand is None
        self.dry_release_co2 = max(co2_values) if co2_values else None
        start = dry and co2_clear and demand is None
        stop = rh >= limit + hysteresis or not drying
        may_change = self.dry_changed_at is None or now - self.dry_changed_at >= hold
        if self.dry_active and (co2_high or demand):
            self.dry_active, self.dry_changed_at = False, now
            self.dry_co2_stopped = self.dry_co2_stopped or co2_high
        elif not self.dry_active and start and may_change:
            self.dry_active, self.dry_changed_at, self.dry_co2_stopped = True, now, False
        elif self.dry_active and stop and may_change:
            self.dry_active, self.dry_changed_at = False, now
        if co2_high and not self.dry_active:
            # CO2 over its setpoint while the limit is off counts as a CO2 stop
            # too: a (re)start waits for setpoint - hysteresis.
            self.dry_co2_stopped = True
        if self.dry_active:
            self.dry_state = "held" if stop else "active"
        elif dry and demand:
            self.dry_state = "humidity_demand"
        elif dry and co2_high:
            self.dry_state = "co2_high"
        elif dry and not co2_clear:
            self.dry_state = "co2_wait"  # under the setpoint, waiting for the restart limit
        else:
            self.dry_state = "waiting" if start else "standby"
        return self.dry_active

    def _dry_floor_level(self, d: dict) -> int:
        """The lowest level the dry-air tiers may cap at: never under the house minimum.

        max(dry_min_level, local_min_level, the house sizing minimum when the
        base level follows the house size). The Auto clamp and the sizing floor
        later in the overlay enforce the same, so this only makes it visible.
        """
        floor = max(int(d.get("dry_min_level") or 1), int(d["local_min_level"]))
        if d.get("sizing_enabled"):
            try:
                floor = max(floor, int(self.config.airflow_plan()["min_level"]))
            except (KeyError, TypeError, ValueError):
                pass
        return min(floor, self.config.max_level())

    def _dry_dynamic(self, d: dict, now: float) -> bool:
        """Dynamic dry-air protection: RH tiers, only while the outdoor air dries the house.

        Inputs: the HA rooms' RH (winter_rh_basis) and their average
        temperature; outdoor temperature and RH from the runtime
        (dry_outdoor_*: the Udeluft room, else T1 with the HA weather). Any of
        the four missing or stale switches it off at once. The unit's own RH is
        never used.

        Tier 1 below dry_tier1_rh, tier 2 below dry_tier2_rh; the held tier
        (and the stricter one) ends only at its limit + dry_dynamic_rh_hysteresis.
        The tiers only apply while indoor minus outdoor absolute humidity is at
        least dry_ah_margin_gm3; that ends under margin - dry_ah_hysteresis_gm3
        and always at 0 or less (the outdoor air then does not dry anything).
        Tier changes hold for dry_hold_minutes. CO2 over its setpoint and any
        humidity demand (bathroom drying, a humid HA room, the unit's own RH
        demand) release it at once; after a CO2 release it only starts again
        with CO2 at or under setpoint - hysteresis, as the plain limit does.
        """
        hold = int(d.get("dry_hold_minutes", 10)) * 60
        rh = self.ha_indoor_rh()
        t_in = self.external.get("indoor_temperature_average")
        t_out = self.external.get("dry_outdoor_temperature")
        rh_out = self.external.get("dry_outdoor_rh")
        ah_in = absolute_humidity(float(t_in), rh) if _is_number(t_in) and rh is not None else None
        ah_out = (absolute_humidity(float(t_out), float(rh_out))
                  if _is_number(t_out) and _is_number(rh_out) else None)
        diff = ah_in - ah_out if ah_in is not None and ah_out is not None else None
        self.dry_air_values = {
            "indoor_rh": rh, "indoor_temperature": float(t_in) if _is_number(t_in) else None,
            "outdoor_temperature": float(t_out) if _is_number(t_out) else None,
            "outdoor_rh": float(rh_out) if _is_number(rh_out) else None,
            "indoor_ah": ah_in, "outdoor_ah": ah_out, "difference": diff,
        }
        floor = self._dry_floor_level(d)
        self.dry_floor = floor
        if diff is None:
            missing = [label for label, value in (
                ("fugt inde", rh), ("temperatur inde", t_in), ("temperatur ude", t_out), ("fugt ude", rh_out))
                if not _is_number(value)] or ["gyldige målinger"]
            if self.dry_tier:
                self.dry_changed_at = now
            self.dry_tier, self.dry_ah_open, self.dry_active, self.dry_co2_stopped = 0, False, False, False
            self.dry_co2_lift = False
            self.dry_state = "sensor_missing"
            self.dry_reason = "Slået fra: mangler friske målinger (" + ", ".join(missing) + ")"
            return False

        margin = float(d["dry_ah_margin_gm3"])
        if self.dry_ah_open:
            if diff <= 0 or diff < margin - float(d["dry_ah_hysteresis_gm3"]):
                self.dry_ah_open = False
        elif diff > 0 and diff >= margin:
            self.dry_ah_open = True

        current = self.dry_tier
        hysteresis = float(d["dry_dynamic_rh_hysteresis"])
        tier1_rh, tier2_rh = float(d["dry_tier1_rh"]), float(d["dry_tier2_rh"])
        if rh < tier1_rh + (hysteresis if current == 1 else 0.0):
            raw = 1
        elif rh < tier2_rh + (hysteresis if current in (1, 2) else 0.0):
            raw = 2
        else:
            raw = 0
        wanted = raw if self.dry_ah_open else 0

        if rh >= tier2_rh + hysteresis:
            self.dry_co2_stopped = False  # no longer dry: the next start is a first start
        setpoint, co2_hysteresis = int(d["co2_setpoint"]), int(d["co2_hysteresis"])
        soft = bool(d.get("dry_co2_soft_enabled"))
        # Soft CO2 (optional): only CO2 over dry_co2_release_ppm releases the protection;
        # between the setpoint and that limit CO2 lifts the cap by one step.
        co2_limit = max(int(d["dry_co2_release_ppm"]), setpoint) if soft else setpoint
        co2_start_limit = co2_limit - co2_hysteresis if self.dry_co2_stopped else co2_limit
        co2_values = [float(co2) for co2 in (self.measurements.get("co2"), self.external.get("max_room_co2"))
                      if _is_number(co2)]
        co2_high = any(co2 > co2_limit for co2 in co2_values)
        co2_clear = all(co2 <= co2_start_limit for co2 in co2_values)
        co2_max = max(co2_values) if co2_values else None
        if not soft or co2_max is None:
            self.dry_co2_lift = False
        elif co2_max > setpoint:
            self.dry_co2_lift = True
        elif co2_max <= setpoint - co2_hysteresis:
            self.dry_co2_lift = False
        demand = self._humidity_demand(d, now)
        may_change = self.dry_changed_at is None or now - self.dry_changed_at >= hold

        new = current
        if current and (co2_high or demand):
            new = 0  # air quality first: released at once, whatever the hold
            self.dry_co2_stopped = self.dry_co2_stopped or co2_high
        elif wanted != current and may_change:
            if current:
                new = wanted
            elif co2_clear and demand is None:
                new = wanted
                self.dry_co2_stopped = False
        if new != current:
            self.dry_tier, self.dry_changed_at = new, now
        if co2_high and not new:
            self.dry_co2_stopped = True
        self.dry_active = new > 0

        def tier_cap(tier: int) -> tuple[int, int]:
            level = int(d["dry_tier1_level"] if tier == 1 else d["dry_tier2_level"])
            return level, max(level, floor)

        lift = 1 if self.dry_co2_lift else 0

        self.dry_rooms_dry = wanted > 0 and demand is None
        self.dry_release_co2 = max(co2_values) if co2_values else None
        if wanted:
            self.dry_release_base = tier_cap(wanted)[1] + 1 + lift
        rh_text, diff_text = f"{rh:.0f} %", _danish_number(round(diff, 1), 1)
        if new:
            self.dry_tier_level, base_cap = tier_cap(new)
            self.dry_cap = min(self.config.max_level(), base_cap + lift)
            text = (f"Tør luft: {rh_text} fugt inde, udeluften er {diff_text} g/m³ tørrere"
                    f" – højst trin {self.dry_cap}")
            if self.dry_tier_level < floor:
                text += f" (trin {self.dry_tier_level} er hævet til husets mindste ventilation, trin {floor})"
            if lift and co2_max is not None:
                text += f" · CO₂ {co2_max:.0f} ppm løfter ét trin"
            if wanted != new:
                self.dry_state = "held"
                text += " · venter på mindste tid før skift"
            else:
                self.dry_state = "active"
            self.dry_reason = text
        elif raw and not self.dry_ah_open:
            self.dry_state = "not_drying"
            self.dry_reason = (f"Ikke aktiv: udeluften tørrer ikke huset nok (forskel {diff_text} g/m³,"
                               f" skal være mindst {_danish_number(margin, 1)})")
        elif wanted and demand:
            self.dry_state = "humidity_demand"
            self.dry_reason = {"bathroom_drying": "Slået fra: et badeværelse tørres ud",
                               "ha_humidity": "Slået fra: der er høj fugt i et rum",
                               "local_rh": "Slået fra: anlæggets egen fugtstyring beder om udluftning"}[demand]
        elif wanted and co2_high:
            self.dry_state = "co2_high"
            self.dry_reason = f"Slået fra: CO₂ {max(co2_values):.0f} ppm er over {co2_limit} ppm"
        elif wanted and not co2_clear:
            self.dry_state = "co2_wait"
            self.dry_reason = f"Venter: CO₂ skal ned på {co2_start_limit} ppm, før den starter igen"
        elif wanted:
            self.dry_state = "waiting"
            self.dry_reason = "Starter, når mindste tid før skift er gået"
        else:
            self.dry_state = "standby"
            self.dry_reason = f"Ikke aktiv: {rh_text} fugt inde er nok (grænse {tier2_rh:.0f} %)"
        return self.dry_active

    # CO2 above the setpoint must climb this far below a step's start before the cap falls back.
    DRY_RELEASE_HYSTERESIS_PPM = 50

    def _dry_release_cap(self, d: dict) -> int | None:
        """The highest level while the dry-air limit lets go in steps, or None for no cap.

        Only with dry_release_steps_enabled, while the limit itself is off and
        the rooms are still dry with no humidity demand (see _dry_air). From
        dry_max_level + 1 one step per dry_release_step_ppm of CO2 over its
        setpoint, so CO2 760 -> 810 gives level 3, not a jump from 2 to 5.
        A lower cap needs CO2 DRY_RELEASE_HYSTERESIS_PPM under the step's start.
        """
        co2 = self.dry_release_co2
        if (not d.get("dry_release_steps_enabled") or self.dry_active or not self.dry_rooms_dry
                or co2 is None):
            self.dry_release_cap = None
            return None
        step = max(50, int(d.get("dry_release_step_ppm") or 200))
        over = float(co2) - int(d["co2_setpoint"])
        base = (self.dry_release_base if self.dry_release_base is not None
                else int(d["dry_max_level"]) + 1)

        def cap_at(value: float) -> int:
            return base + max(0, math.floor(value / step))

        cap = cap_at(over)
        previous = self.dry_release_cap
        if previous is not None and cap < previous:
            cap = max(cap, min(previous, cap_at(over + self.DRY_RELEASE_HYSTERESIS_PPM)))
        self.dry_release_cap = cap
        return cap

    # Automatic control only: these overlays and manual control choose the
    # level themselves, so their changes are never counted as flapping.
    _UNCOUNTED_FLAGS = ("vacation_active", "quick_boost_active", "cool_boost_active",
                        "standby_active", "bonfire_active")

    def _count_level_change(self, level: int | None, flags: dict[str, bool], now: float) -> int:
        """Note an automatic level change; returns the automatic changes in the last hour.

        A change is counted only when the level changes between two automatic
        ticks with the same context: never in manual mode (manual level, the
        max-level timeout), quick boost, Køl, standby, bonfire or vacation, and
        not when a schedule period or the night window starts or ends.
        """
        d = self.config.data
        automatic = d["mode"] != "manual" and not any(flags.get(key) for key in self._UNCOUNTED_FLAGS)
        context = (automatic, d["mode"], bool(flags.get("night_active")), self._schedule_key, bool(d.get("fireplace")))
        if (automatic and self._counted_level is not None and level != self._counted_level
                and context == self._counted_context):
            self.level_change_times.append(now)
        self._counted_level, self._counted_context = level, context
        while self.level_change_times and now - self.level_change_times[0] >= 3600:
            self.level_change_times.popleft()
        return len(self.level_change_times)

    def level_change_quiet_seconds(self, now: float) -> float | None:
        """Seconds since the last counted automatic level change (None without one this hour)."""
        return round(now - self.level_change_times[-1], 1) if self.level_change_times else None

    def winter_indoor_rh(self) -> tuple[float | None, str | None]:
        """Indoor RH for winter mode: the fresh HA rooms (average or lowest), else none.

        The unit's own extract RH reads far too low (13-17 % against ~40 % in
        the rooms), so without fresh HA rooms winter mode reports sensor_missing.
        """
        rh = self.ha_indoor_rh()
        if rh is None:
            return None, None
        return rh, "ha_lowest" if self.config.data.get("winter_rh_basis") == "lowest" else "ha_average"

    def _ha_live(self, d: dict, now: float) -> bool:
        seen = d.get("ha_last_seen")
        lease = min(int(d["ha_timeout_seconds"]), int(d.get("ha_valid_for_seconds", d["ha_timeout_seconds"])))
        return d["mode"] == "smart_auto" and bool(seen) and now - float(seen) <= lease + HA_LEASE_GRACE_SECONDS

    def _winter_pause_trigger(self, d: dict, now: float) -> str | None:
        """Air-quality demand that suspends winter mode: it then ventilates as usual."""
        co2_limit = int(d["co2_setpoint"]) + int(d["co2_hysteresis"])
        for co2 in (self.measurements.get("co2"), self.external.get("max_room_co2")):
            if isinstance(co2, (int, float)) and co2 > co2_limit:
                return f"CO2 {co2:.0f} ppm over {co2_limit} ppm"
        if self._ha_live(d, now):
            if d.get("ha_bathroom_drying"):
                return "badeværelse tørres ud"
            if d.get("ha_humidity_demand"):
                return "høj fugt i et rum"
        rh = self.measurements.get("rh")
        if isinstance(rh, (int, float)) and rh > float(d["rh_setpoint"]):
            return f"fugt {rh:.0f} % over {float(d['rh_setpoint']):.0f} %"
        return None

    def _update_winter(self, now: float) -> None:
        """Winter dry-air mode: lower the base level while it is cold outside and dry inside.

        Start when T1 <= winter_outdoor_max and indoor RH < winter_rh_limit; stop when
        T1 > max + hysteresis or RH >= limit + hysteresis. The state changes at most once
        per winter_hold_minutes, except that missing sensors stop it at once. Clear CO2 or
        humidity demand pauses it for at least the same time, so normal control returns.
        """
        d = self.config.data
        level = int(d["winter_level"])
        self.config.winter_base = None
        if not d.get("winter_dry_enabled"):
            self.winter_active, self.winter_changed_at = False, None
            self.winter_pause_until, self.winter_pause_reason = 0.0, None
            self.winter_state = "disabled"
            return
        if d.get("dry_protection_enabled") and d.get("dry_dynamic_enabled"):
            # The dynamic dry-air protection takes over (tiers by RH and the
            # absolute humidity): one mechanism, not two pulling at the level.
            self.winter_active, self.winter_changed_at = False, None
            self.winter_pause_until, self.winter_pause_reason = 0.0, None
            self.winter_state = "dynamic"
            return
        hold = int(d["winter_hold_minutes"]) * 60
        outdoor = self.measurements.get("outdoor")
        rh, _source = self.winter_indoor_rh()
        if not isinstance(outdoor, (int, float)) or rh is None:
            if self.winter_active:
                self.winter_changed_at = now
            self.winter_active = False
            self.winter_state = "sensor_missing"
            return
        outdoor_max = float(d["winter_outdoor_max"])
        rh_limit = float(d["winter_rh_limit"])
        start = outdoor <= outdoor_max and rh < rh_limit
        stop = outdoor > outdoor_max + float(d["winter_outdoor_hysteresis"]) or rh >= rh_limit + float(d["winter_rh_hysteresis"])
        may_change = self.winter_changed_at is None or now - self.winter_changed_at >= hold
        if not self.winter_active and start and may_change:
            self.winter_active, self.winter_changed_at = True, now
        elif self.winter_active and stop and may_change:
            self.winter_active, self.winter_changed_at = False, now
        if not self.winter_active:
            self.winter_pause_until, self.winter_pause_reason = 0.0, None
            self.winter_state = "waiting" if start else "standby"
            return
        trigger = self._winter_pause_trigger(d, now)
        if trigger:
            self.winter_pause_until, self.winter_pause_reason = now + hold, trigger
        if now < self.winter_pause_until:
            self.winter_state = "paused"
            return
        self.winter_pause_reason = None
        if d["mode"] == "manual" or level >= self.config.configured_normal_level():
            self.winter_state = "manual" if d["mode"] == "manual" else "base_already_low"
            return
        self.winter_state = "active"
        self.config.winter_base = level

    def _winter_text(self) -> str:
        rh, _source = self.winter_indoor_rh()
        outdoor = self.measurements.get("outdoor")
        rh_text = f"{rh:.0f} %" if rh is not None else "?"
        outdoor_text = f"{outdoor:.0f} °C" if isinstance(outdoor, (int, float)) else "?"
        return f"inde {rh_text}, ude {outdoor_text}"

    def _apply_winter(self, level: int, reason: str) -> tuple[int, str]:
        """Hold the auto level at least at the winter base and explain it."""
        if self.winter_state == "paused" and self.winter_pause_reason:
            return level, f"{reason}; vinter (tør luft) på pause: {self.winter_pause_reason}"
        if self.winter_state != "active":
            return level, reason
        base = int(self.config.data["winter_level"])
        if level < base:
            level = base
            self.current_auto_level = level
        if level == base:
            return level, f"Vinter: tør luft – holder trin {base} ({self._winter_text()})"
        return level, f"Vinter: tør luft – grundtrin {base} ({self._winter_text()}); {reason}"

    def frost_readings(self) -> list[tuple[str, float, float]]:
        """Known (label, temperature, limit) values the frost lock looks at."""
        d = self.config.data
        outdoor_limit = float(d["frost_lock_outdoor_c"])
        if "outdoor_temperature" in self.external:
            outdoor = self.external.get("outdoor_temperature")
        else:
            outdoor = self.measurements.get("outdoor")
        readings = []
        for label, value, limit in (
            ("ude", outdoor, outdoor_limit),
            ("loft", self.external.get("attic_temperature"), outdoor_limit),
            ("frostføler", self.external.get("afterheat_frost_temperature"), float(d["frost_lock_coil_c"])),
        ):
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)):
                readings.append((label, float(value), limit))
        return readings

    def _update_frost_lock(self, now: float, clock: float | None = None) -> bool:
        """Decide the afterheat frost lock; the one place this is decided.

        Any known value at or below its limit for FROST_LOCK_ENGAGE_SECONDS
        latches the frost state (a lone glitched reading does not).
        It releases only when every known value has been above its limit plus
        FROST_LOCK_RELEASE_MARGIN_C for FROST_LOCK_RELEASE_SECONDS without a
        break, and only while the outdoor air (T1 or the attic) is known.
        With no known value the previous state is kept (fail-safe).
        The lock acts only when enabled and the coil is a water coil.
        """
        if clock is None:
            clock = getattr(self, "_interval_now", None)
        clock = now if clock is None else clock
        self._frost_clock_now = clock
        d = self.config.data
        readings = self.frost_readings()
        latched = d.get("frost_lock_latched") is True
        cold = [(label, value, limit) for label, value, limit in readings if value <= limit]
        reason: str | None = None
        if not cold:
            self.frost_cold_since = None
        if cold:
            self.frost_release_since = None
            reason = "Frost: " + ", ".join(
                f"{label} {_danish_number(value, 1)} °C (grænse {_danish_number(limit)} °C)" for label, value, limit in cold)
            if not latched:
                if self.frost_cold_since is None:
                    self.frost_cold_since = clock
                if clock - self.frost_cold_since >= self.frost_engage_seconds:
                    latched = True
                    self.frost_cold_since = None
        elif not readings:
            self.frost_release_since = None
            reason = "Frost: ingen temperaturer kendt, frostlåsen holdes for en sikkerheds skyld"
        elif latched:
            waiting = [(label, value, limit + FROST_LOCK_RELEASE_MARGIN_C) for label, value, limit in readings
                       if value <= limit + FROST_LOCK_RELEASE_MARGIN_C]
            air_known = any(label in ("ude", "loft") for label, _value, _limit in readings)
            if not air_known:
                # A warm frost sensor alone cannot prove thaw: without T1 (or the
                # attic) the lock stays and the half hour does not run.
                self.frost_release_since = None
                reason = "Frost: venter på udetemperatur (T1)"
            elif waiting:
                self.frost_release_since = None
                reason = "Frost: venter på tø, " + ", ".join(
                    f"{label} {_danish_number(value, 1)} °C (slipper over {_danish_number(limit)} °C)"
                    for label, value, limit in waiting)
            else:
                if self.frost_release_since is None:
                    self.frost_release_since = clock
                left = FROST_LOCK_RELEASE_SECONDS - (clock - self.frost_release_since)
                if left <= 0:
                    latched = False
                    self.frost_release_since = None
                else:
                    reason = f"Frost: tøvejr, frostlåsen slipper om ca. {max(1, math.ceil(left / 60))} min"
        else:
            self.frost_release_since = None
        if latched != (d.get("frost_lock_latched") is True):
            d["frost_lock_latched"] = latched
            d["updated_at"] = now
            try:
                self.config.save()
            except OSError:
                pass  # the latch still works in memory
        self.frost_lock_active = self.config.frost_lock_in_force()
        self.frost_lock_reason = reason if self.frost_lock_active else None
        return self.frost_lock_active

    def _afterheat_override(self) -> float | None:
        """Valid experiment/tool setpoint from AFTERHEAT_OVERRIDE_PATH, else None."""
        path = AFTERHEAT_OVERRIDE_PATH
        try:
            stat = path.stat()
        except OSError:
            self._override_cache = None
            return None
        cached = getattr(self, "_override_cache", None)
        if cached is None or cached[0] != (stat.st_mtime_ns, stat.st_size):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                value = float(data["setpoint"])
                expires = float(data["expires_at"])
            except (OSError, ValueError, KeyError, TypeError):
                value, expires = None, 0.0
            cached = self._override_cache = ((stat.st_mtime_ns, stat.st_size), value, expires)
        _key, value, expires = cached
        wall = time.time()
        if value is None or not 10.0 <= value <= 35.0 or not wall < expires <= wall + AFTERHEAT_OVERRIDE_MAX_SECONDS:
            return None
        return round(value * 256) / 256

    def _night_outdoor(self) -> float | None:
        """Outdoor air for the night cooling: the outdoor room (e.g. the house's own sensor), else T1."""
        for key in ("outdoor_air_temperature", "outdoor_temperature"):
            value = self.external.get(key)
            if _is_number(value):
                return float(value)
        return None

    def _night_indoor(self) -> float | None:
        """The rooms' average (comfort rooms), else the room temperature used for free cooling."""
        for value in (self.external.get("rooms_temperature"), self.measurements.get("room")):
            if _is_number(value):
                return float(value)
        return None

    def _today_high(self) -> float | None:
        """Today's high from Home Assistant (DMI), else the highest outdoor value seen today."""
        value = self.external.get("today_high_c")
        if _is_number(value):
            return float(value)
        return self.outdoor_history.high

    def _night_cool_blocker(self, d: dict, now_ts: float, flags: dict) -> str | None:
        """What stops the night cooling right now (fireplace, standby, vacation, frost lock, ...)."""
        standby_until = d.get("standby_until")
        bonfire_until = d.get("bonfire_until")
        if d.get("standby") is True and (standby_until is None or float(standby_until) > now_ts):
            return "anlægget er slukket"
        if bonfire_until and float(bonfire_until) > now_ts:
            return "bål i haven"
        if d.get("fireplace"):
            return "pejs"
        if flags.get("vacation_active"):
            return "ferie"
        if d["mode"] == "manual":
            return "manuelt valgt trin"
        if self.frost_lock_active:
            return "frostsikring"
        return None

    def _afterheat_saturation(self) -> str | None:
        """'high' when the afterheat is at full output and the supply is still too cold, 'low' when off and too warm."""
        level = self.last_pilot.get("afterheat_level")
        error = self.last_pilot.get("afterheat_control_error")
        if not _is_number(level) or not _is_number(error):
            return None
        if int(level) >= AFTERHEAT_LEVEL_MAX and float(error) < -SATURATION_MARGIN_K:
            return "high"
        if int(level) <= 0 and float(error) > SATURATION_MARGIN_K:
            return "low"
        return None

    def _afterheat_setpoint(self, now: float, locked: str | None = None) -> tuple[int | float, str]:
        """The afterheat target: the fixed setpoint, or the supply target the rooms want (room_cascade)."""
        d = self.config.data
        fixed = _clean_afterheat_setpoint(d["afterheat_setpoint"])
        drop = self._vacation_afterheat_drop(now)
        holiday = f" · ferie: {_danish_number(drop, 1)} K koldere" if drop else ""
        if not d.get("afterheat_room_enabled"):
            self.room_cascade.reset()
            self.afterheat_room_output = None
            self.afterheat_room_state = "disabled"
            if drop:
                return round(max(10.0, float(fixed) - drop), 1), "Fast setpunkt" + holiday
            return fixed, "Fast setpunkt"
        room = self.external.get("afterheat_room_temperature")
        room = float(room) if _is_number(room) else None
        offset = float(d.get("afterheat_room_offset_k") or 0.0)
        if self.room_cascade.exact is None:
            saved = d.get("afterheat_room_value")
            base = float(saved) if _is_number(saved) else float(fixed) - offset
        else:
            base = float(fixed)
        # On vacation the rooms' target is lowered, so the cascade itself asks for cooler air.
        settings = {**d, "afterheat_room_target": float(d["afterheat_room_target"]) - drop} if drop else d
        wanted = self.room_cascade.update(now=now, room=room, settings=settings, base=base, locked=locked,
                                          saturated=self._afterheat_saturation())
        self.afterheat_room_state = self.room_cascade.state
        if wanted is None:
            self.afterheat_room_output = None
            if drop:
                return round(max(10.0, float(fixed) - drop), 1), "Ingen rumtemperatur: fast setpunkt" + holiday
            return fixed, "Ingen rumtemperatur: fast setpunkt"
        # The setpoint at the unit: the air the rooms should get plus what the duct loses.
        value = round(min(35.0, max(10.0, wanted + offset)), 1)
        self.afterheat_room_output = value
        exact = round(float(self.room_cascade.exact), 2)
        if exact != d.get("afterheat_room_value") and now - self.afterheat_room_saved_at >= 1800:
            # Saved at most every 30 minutes, so a restart continues from about here.
            d["afterheat_room_value"] = exact
            self.afterheat_room_saved_at = now
            self.config.save()
        text = f"Mål fra rummene: {_danish_number(room, 1)} °C i rummene → indblæsning {_danish_number(wanted, 1)} °C"
        if offset:
            text += f" ({'+' if offset > 0 else '−'}{_danish_number(abs(offset), 1)} K kanaltab = {_danish_number(value, 1)} °C ved anlægget)"
        band = float(d.get("afterheat_room_band_k") or 0.0)
        extra = {"locked": f" · holdes: {locked}", "saturated": " · eftervarmen kan ikke følge med",
                 "at_limit": " · ved grænsen",
                 "in_band": f" · inden for ±{_danish_number(band, 1)} K af {_danish_number(float(settings['afterheat_room_target']), 1)} °C"}.get(
                     self.room_cascade.state, "")
        return value, text + extra + holiday

    def _vacation_afterheat_drop(self, now: float) -> float:
        """How many degrees vacation lowers the afterheat target right now (0 when off)."""
        d = self.config.data
        if not d.get("vacation_afterheat_enabled") or not self.config.vacation_running(now):
            return 0.0
        try:
            return min(8.0, max(0.0, float(d.get("vacation_afterheat_drop_k") or 0.0)))
        except (TypeError, ValueError):
            return 0.0

    def _afterheat_pilot(self, now: float, d: dict, target: float, afterheat_on: bool, frost_lock: bool,
                         frost_floor: float, flags: dict, fans_stopped: bool, override: bool) -> dict[str, object]:
        """Run the Pi afterheat control on the latest HAC1 values (see afterheat_pilot)."""
        values = self.afterheat_inputs

        def age(key: str) -> float | None:
            stamp = values.get(key)
            return now - float(stamp) if isinstance(stamp, (int, float)) and not isinstance(stamp, bool) else None

        return self.afterheat_pilot.update(PilotInputs(
            now=now, mode=str(d.get("afterheat_control_mode") or "native"),
            afterheat_on=afterheat_on, target=target,
            frost_lock=frost_lock, frost_floor=frost_floor,
            level=values.get("level"), level_age=age("level_at"),
            level_changed_at=values.get("level_changed_at"),
            t2ah=values.get("t2ah"), t2ah_age=age("t2ah_at"), t2ah_raw=values.get("t2ah_raw"),
            fan_supply=values.get("fan_supply"), water_flow=values.get("water_flow"),
            before_heater=values.get("before_heater"), frost_sensor=values.get("frost_sensor"),
            outdoor=self.external.get("outdoor_temperature"), extract=self.external.get("extract_temp"),
            master=values.get("master") is True,
            cool_boost=bool(flags.get("cool_boost_active") or flags.get("night_cool_active")),
            free_cooling=bool(flags.get("cooling_active")),
            fans_stopped=fans_stopped, override=override,
            gain=float(d.get("afterheat_pi_gain", PI_GAIN_DEFAULT)),
            integral_minutes=float(d.get("afterheat_pi_integral_minutes", PI_INTEGRAL_MINUTES_DEFAULT)),
            band=float(d.get("afterheat_pi_band", PI_BAND_DEFAULT)),
            min_dwell_ticks=int(d.get("afterheat_pi_min_dwell_ticks", PI_MIN_DWELL_TICKS_DEFAULT)),
            algorithm=str(d.get("afterheat_algorithm") or AFTERHEAT_ALGORITHM_DEFAULT),
        ))

    def afterheat_native_setpoint(self, lock_timeout: float | None = None) -> int | None:
        """The plain whole-degree target HAC1 should keep on its own (gateway stop), or None.

        With lock_timeout the engine lock is only waited for that long: a
        stalled controller thread may hold it on the watchdog exit path. Then
        the configured setpoint (with the frost floor while frost is latched)
        is used instead of the last resolved one.
        """
        if lock_timeout is None:
            with self.lock:
                last = dict(self.last_afterheat)
        elif self.lock.acquire(timeout=max(0.0, lock_timeout)):
            try:
                last = dict(self.last_afterheat)
            finally:
                self.lock.release()
        else:
            if dict(self.last_afterheat).get("command") is None:
                return None
            return self.fallback_native_setpoint()
        if last.get("command") is None:
            return None
        return last.get("native")

    def fallback_native_setpoint(self) -> int:
        """The configured afterheat setpoint in whole degrees, raised to the frost floor while frost is latched.

        Reads the config without its lock (exit path only).
        """
        try:
            native = math.floor(float(self.config.data.get("afterheat_setpoint", 20)) + 0.5)
        except (TypeError, ValueError):
            native = 20
        if self.config.frost_lock_in_force():
            native = max(native, math.ceil(self.config.frost_lock_floor()))
        return int(min(35, max(10, native)))

    def resolve(self, now: float | None = None) -> dict[str, object]:
        # Intervals (the frost lock's 2 min engage and 30 min release) run on
        # the monotonic clock in production: the Pi has no RTC and NTP steps
        # the wall clock after boot. A caller-given ``now`` drives both.
        interval_now = time.monotonic() if not now else now
        now = now or time.time()
        with self.config.lock, self.lock:
            self.config._expire_fireplace(now)
            self.config._expire_vacation(now)
            self.config._expire_quick_boost(now)
            self.config._expire_cool_boost(now)
            self.config._expire_max_level(now)
            d = self.config.data
            self._update_winter(now)
            self._interval_now = interval_now
            try:
                frost_lock = self._update_frost_lock(now)
            finally:
                self._interval_now = None
            if frost_lock and d.get("cool_boost_until"):
                # Køl lets freezing air past the coil: it ends when the frost lock engages.
                d["cool_boost_until"], d["cool_boost_minutes"] = None, 0
                d["updated_at"] = now
                self.config.save()
                self.cool_stopped_by_frost = True  # the runtime writes it to the change log
            if d["mode"] == "manual":
                source, level, reason = "manual", int(d["manual_level"]), "Manuelt valgt niveau"
                if self.config.top_hold_active() and d.get("max_level_until"):
                    remaining = max(0, math.ceil((float(d["max_level_until"]) - now) / 60))
                    reason = (f"Trin {level} (maksimum) · tilbage til trin "
                              f"{self.config.top_return_level()} om ca. {remaining} min")
            elif d["mode"] == "smart_auto":
                seen = d.get("ha_last_seen")
                lease = min(int(d["ha_timeout_seconds"]), int(d.get("ha_valid_for_seconds", d["ha_timeout_seconds"])))
                # A short gap in HA's pushes keeps the last Smart Auto level for a
                # grace period instead of flipping to Local Auto for a few seconds.
                if seen and now - float(seen) <= lease + HA_LEASE_GRACE_SECONDS:
                    wanted = int(d["ha_requested_level"])
                    level, held = self._stabilize_auto_level(
                        wanted, now, urgent=bool(d.get("ha_bathroom_drying")))
                    source = "ha_smart"
                    reason = str(d.get("ha_reason") or f"HA demand: {d['ha_demand']}")
                    if held:
                        reason = f"{reason}; {held}"
                    if now - float(seen) > lease:
                        reason = f"{reason}; venter på nye data fra HA"
                    level, reason = self._apply_winter(level, reason)
                else:
                    level, reason = self._apply_winter(*self._local_auto_level(now))
                    source = "local_fallback"
                    reason = f"HA offline; {reason}"
            else:
                level, reason = self._apply_winter(*self._local_auto_level(now))
                source = "local_auto"

            if self.outdoor_history.add(self._night_outdoor(), now):
                # A day completed: keep the daily means over a restart.
                d["outdoor_daily_means"] = [list(day) for day in self.outdoor_history.days]
                self.config.save()
            level, source, reason, effective_bypass, flags = self._automation_overlay(level, source, reason, now)
            # With the fans stopped (standby or bonfire) the water coil would heat
            # still air, so the afterheat is switched off and restored afterwards.
            # The frost lock keeps it on whatever was chosen.
            fans_stopped = bool(flags.get("standby_active") or flags.get("bonfire_active"))
            afterheat_on = frost_lock or (bool(d["afterheat_enabled"]) and not fans_stopped)
            override = self._afterheat_override() if afterheat_on else None
            frost_floor = self.config.frost_lock_floor()
            # Optional: free cooling holds the afterheat down like Køl, so the bypass air is not heated again.
            free_cool_hold = bool(flags.get("cooling_active") and d.get("cooling_afterheat_hold_enabled"))
            cooling_down = bool(flags.get("cool_boost_active") or flags.get("night_cool_active") or free_cool_hold)
            locked = (
                "eftervarmen er slukket" if not afterheat_on
                else "Køl" if flags.get("cool_boost_active")
                else "natkøling" if flags.get("night_cool_active")
                else "frikøling" if free_cool_hold
                else "forsøg med setpunkt" if override is not None
                else "frostsikring" if frost_lock and self.afterheat_room_output is not None
                and self.afterheat_room_output < frost_floor
                else None)
            afterheat_setpoint, afterheat_reason = self._afterheat_setpoint(now, locked)
            afterheat_target = float(afterheat_setpoint)
            if cooling_down:
                # Cool lets cold outdoor air in on purpose: the afterheat must not warm it up again.
                afterheat_setpoint = COOL_BOOST_AFTERHEAT_SETPOINT
                afterheat_reason = ("Køl: eftervarme holdt nede" if flags.get("cool_boost_active")
                                    else "Natkøling: eftervarme holdt nede" if flags.get("night_cool_active")
                                    else "Frikøling: eftervarme holdt nede")
            if frost_lock:
                afterheat_target = max(afterheat_target, frost_floor)
            if frost_lock and afterheat_setpoint < frost_floor:
                afterheat_setpoint = math.ceil(frost_floor)
                afterheat_reason = f"Frostsikring: mindst {_danish_number(frost_floor)} °C, mens det fryser"
            if override is not None:
                afterheat_setpoint = max(override, frost_floor) if frost_lock else override
                afterheat_reason = f"Forsøg: setpunkt {_danish_number(afterheat_setpoint, 2)} °C"
            elif not float(afterheat_setpoint).is_integer():
                # HAC1 uses whole degrees: natively the target is rounded.
                afterheat_setpoint = math.floor(float(afterheat_setpoint) + 0.5)
            pilot = self._afterheat_pilot(now, d, afterheat_target, afterheat_on, frost_lock, frost_floor,
                                          flags, fans_stopped, override is not None)
            self.last_pilot = dict(pilot)
            afterheat_command = afterheat_setpoint if afterheat_on else None
            if pilot["afterheat_control_active"] == "pi" and pilot["afterheat_hac1_setpoint"] is not None:
                afterheat_command = int(pilot["afterheat_hac1_setpoint"])
                if frost_lock:
                    afterheat_command = max(afterheat_command, math.ceil(frost_floor))
            # What HAC1 is given (also natively); a fractional override is not a whole degree.
            pilot["afterheat_hac1_setpoint"] = (
                int(afterheat_command) if afterheat_command is not None and float(afterheat_command).is_integer() else None)
            d["effective_source"] = source
            d["effective_level"] = level
            d["effective_reason"] = reason
            d["updated_at"] = now
            level_changes = self._count_level_change(level, flags, now)
            profile = d["profiles"].get(level) if level else None
            if flags.get("bonfire_active"):
                profile = dict(BONFIRE_PROFILE)
            if flags.get("standby_active"):
                profile = dict(STANDBY_PROFILE)
            start_delay = int(d["cooling_start_delay_seconds"])
            qualifying_remaining = None
            if self.cooling_reason == "qualifying" and self.cooling_qualifying_since is not None:
                qualifying_remaining = max(0, math.ceil(start_delay - (now - self.cooling_qualifying_since)))
            min_on_remaining = 0
            if self.cooling_active and self.cooling_last_on_at is not None:
                min_on_remaining = max(0, math.ceil(int(d["cooling_min_on_seconds"]) - (now - self.cooling_last_on_at)))
            min_off_remaining = 0
            if not self.cooling_active and self.cooling_last_off_at is not None:
                min_off_remaining = max(0, math.ceil(int(d["cooling_min_off_seconds"]) - (now - self.cooling_last_off_at)))
            result = {
                **self.config.snapshot(),
                "measurements": dict(self.measurements),
                "effective_profile": dict(profile) if profile else None,
                "effective_bypass": effective_bypass,
                **flags,
                "cooling_state": self.cooling_reason,
                "cooling_qualification_remaining_seconds": qualifying_remaining,
                "cooling_min_on_remaining_seconds": min_on_remaining,
                "cooling_min_off_remaining_seconds": min_off_remaining,
                "last_write_at": self.last_write_at,
                "last_error": self.last_error,
                "write_failures": self.write_failures,
                "retry_limit": self.retry_limit,  # compatibility: fast retry count
                "pending_commands": {key: {"error": error, "attempts": self._failure_counts.get(key, 0),
                                           "next_retry_at": self._retry_at.get(key)}
                                     for key, error in list(self.command_errors.items())},
                "next_retry_at": min(self._retry_at.values(), default=None),
                "controller_uptime_seconds": round(now - self.started_at, 1),
                "afterheat_effective_setpoint": afterheat_setpoint,
                "afterheat_command": afterheat_command,
                **pilot,
                "afterheat_override_setpoint": override,
                "afterheat_frost_lock": frost_lock,
                "afterheat_frost_lock_reason": self.frost_lock_reason,
                "afterheat_frost_lock_floor": frost_floor,
                "afterheat_frost_lock_release_seconds": (
                    max(0, math.ceil(FROST_LOCK_RELEASE_SECONDS - (getattr(self, "_frost_clock_now", now) - self.frost_release_since)))
                    if frost_lock and self.frost_release_since is not None else None),
                "afterheat_room_state": self.afterheat_room_state,
                "afterheat_room_reason": afterheat_reason,
                "afterheat_room_temperature": self.external.get("afterheat_room_temperature"),
                "afterheat_room_supply_target": self.afterheat_room_output if d.get("afterheat_room_enabled") else None,
                "afterheat_room_rooms_target": self.room_cascade.value if d.get("afterheat_room_enabled") else None,
                "afterheat_room_rooms": self.external.get("afterheat_room_rooms"),
                "afterheat_room_saturated": self._afterheat_saturation(),
                **self.night_cooling.result(),
                "night_cool_season_mean": self.outdoor_history.running_mean(),
                "night_cool_outdoor": self._night_outdoor(),
                "night_cool_indoor": self._night_indoor(),
                "night_cool_forecast_high": self.external.get("forecast_high_c"),
                "night_cool_today_high": self._today_high(),
                "indoor_absolute_humidity": _round(self.indoor_absolute_humidity()),
                "outdoor_absolute_humidity": _round(self.outdoor_absolute_humidity()),
                "outdoor_humidity": self.external.get("outdoor_rh"),
                "humidity_drying": self.humidity_dries(self.indoor_absolute_humidity()),
                "winter_dry_active": self.winter_state == "active",
                "winter_dry_state": self.winter_state,
                "winter_dry_pause_reason": self.winter_pause_reason,
                "winter_indoor_rh": _round(self.winter_indoor_rh()[0], 1),
                "winter_indoor_rh_source": self.winter_indoor_rh()[1],
                "dry_protection_state": self.dry_state,
                "dry_protection_indoor_rh": _round(self.ha_indoor_rh(), 1),
                **self._dry_dynamic_snapshot(d),
                "level_changes_last_hour": level_changes,
                "level_change_quiet_seconds": self.level_change_quiet_seconds(now),
            }
            native = math.floor(afterheat_target + 0.5)
            if frost_lock:
                native = max(native, math.ceil(frost_floor))
            self.last_afterheat = {"command": afterheat_command, "native": int(min(35, max(10, native)))}
            return result

    def _call(
        self,
        key: str,
        value: object,
        fn: Callable | None,
        *args,
        refresh_seconds: float | None = None,
        keep_retrying: bool = False,
    ) -> None:
        now = time.time()
        unchanged = self.last_applied.get(key) == value
        refreshed_recently = (
            now - self.last_applied_at.get(key, 0.0) < (refresh_seconds or 0.0)
        )
        if unchanged and (refresh_seconds is None or refreshed_recently):
            # A failed newer request may have been cancelled back to the
            # already acknowledged value. It is no longer a pending fault.
            if self._failure_values.get(key) != value:
                self.command_errors.pop(key, None)
                self._retry_at.pop(key, None)
                self._failure_counts.pop(key, None)
                self.last_error = next(iter(self.command_errors.values()), None)
            return
        if self._failure_values.get(key) != value:
            self._failure_values[key] = value
            self._failure_counts[key] = 0
            self._retry_at[key] = 0.0
        # A retry planned earlier never waits longer than the current cap: the
        # frost lock's 60 s also applies to a retry scheduled before it engaged,
        # and a wall clock stepped back cannot push a retry far out.
        cap = FROST_LOCK_RETRY_MAX_SECONDS if keep_retrying else RETRY_MAX_SECONDS
        if self._retry_at.get(key, 0.0) > now + cap:
            self._retry_at[key] = now + cap
        if now < self._retry_at.get(key, 0.0):
            return
        if fn is None:
            error = RuntimeError(f"Hardware binding mangler: {key}")
            self.last_error = str(error)
            self.command_errors[key] = self.last_error
            self.write_failures += 1
            self._failure_counts[key] = self._failure_counts.get(key, 0) + 1
            self._retry_at[key] = now + min(RETRY_MAX_SECONDS, self.retry_base_seconds * 2 ** min(6, self._failure_counts[key] - 1))
            raise error
        try:
            fn(*args)
        except Exception as error:
            self.last_error = f"{key}: {error}"
            self.command_errors[key] = self.last_error
            self.write_failures += 1
            self._failure_counts[key] = self._failure_counts.get(key, 0) + 1
            delay = min(RETRY_MAX_SECONDS, self.retry_base_seconds * 2 ** min(6, self._failure_counts[key] - 1))
            if keep_retrying:
                delay = min(delay, FROST_LOCK_RETRY_MAX_SECONDS)
            self._retry_at[key] = now + delay
            raise
        else:
            self.last_applied[key] = value
            self.last_applied_at[key] = time.time()
            self.last_write_at = self.last_applied_at[key]
            self.command_errors.pop(key, None)
            self.last_error = next(iter(self.command_errors.values()), None)
            self._failure_counts[key] = 0
            self._retry_at.pop(key, None)

    def _apply_afterheat(self, snapshot: dict[str, object]) -> None:
        command = snapshot.get("afterheat_command")
        frost_lock = snapshot.get("afterheat_frost_lock") is True
        # Whole degrees normally; an override may carry 1/256 °C steps (HAC1 186 = sp*256).
        value = None if command is None else (
            int(command) if float(command).is_integer() else round(float(command) * 256) / 256)
        self._call(
            "afterheat_setpoint",
            value,
            self.hardware.set_afterheat_setpoint,
            value,
            refresh_seconds=4.0,
            keep_retrying=frost_lock,
        )

    def apply(self) -> dict[str, object]:
        """Apply only changed desired values; arbitration is enforced below this layer."""
        snapshot = self.resolve()
        frost_error: Exception | None = None
        if snapshot.get("afterheat_frost_lock") is True:
            # Frost: the afterheat goes first, so a failing write below can never skip it.
            try:
                self._apply_afterheat(snapshot)
            except Exception as error:  # the other writes still run; raised at the end
                frost_error = error
        # Standby and bonfire both switch the unit off with the standby pattern.
        standby = snapshot.get("standby_active") is True or snapshot.get("bonfire_active") is True
        previous_standby = self.last_applied.get("standby")
        if standby or previous_standby is True:
            # The standby pattern replaces the fan pair; leaving it restores the
            # previous pair in the gateway, then the wanted pair is written again.
            self._call("standby", standby, self.hardware.set_standby, standby)
            if previous_standby is True and not standby:
                self.last_applied.pop("fan_pair", None)
        fireplace = bool(snapshot["fireplace"])
        previous_fireplace = self.last_applied.get("fireplace")
        # Resent every minute while on: the gateway's own 15-minute fireplace
        # timer is only a safety net if the controller stops, so it must not end
        # a longer fireplace (auto mode holds it up to fireplace_max_hours).
        self._call("fireplace", fireplace, self.hardware.set_fireplace, fireplace,
                   refresh_seconds=FIREPLACE_REFRESH_SECONDS if fireplace else None)
        if previous_fireplace is True and not fireplace:
            self.last_applied.pop("fan_pair", None)
        bypass = str(snapshot.get("effective_bypass", snapshot["bypass"]))
        if self.hardware.set_bypass is not None:
            self._call("bypass", bypass, self.hardware.set_bypass, bypass)
        profile = snapshot.get("effective_profile")
        if profile and not standby:
            pair = (int(profile["extract"]), int(profile["supply"]))
            self._call("fan_pair", pair, self.hardware.write_fan_pair, *pair)
        if snapshot.get("afterheat_frost_lock") is True:
            if frost_error is not None:
                raise frost_error
        else:
            self._apply_afterheat(snapshot)
        return self.resolve()
