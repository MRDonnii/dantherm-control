"""Read-only analyses of the stored one-minute history (no settings, no hardware).

``afterheat_scale``: what one HAC1 level (0..10 = 0-100 % valve) is worth in
kelvin at each supply fan level, and the return water on each level.
``weekly_health``: the last week's supply-air swings, level changes, command
errors, watchdog restarts and stale measurements.

Both are pure functions of rows from ``HistoryStore.rows`` and small lists the
caller passes, so they run on the request thread and never touch the live
controller loop.
"""
from __future__ import annotations

import math
import statistics
import time
from collections import defaultdict

from afterheat_pilot import FAN_REFERENCE_PERCENT, LEVEL_MAX, WATER_MIN_LIFT_K, WATER_REFERENCE_LIFT_K
from advanced_control import supply_after_core

# A level counts as settled after this many one-minute samples on it at the same fan.
SETTLE_MINUTES = 3
# Fewer settled minutes than this on a level: no value is shown.
MIN_POINTS = 3
# Consecutive samples further apart than this are a gap.
SAMPLE_GAP_S = 90
# Afterheat contexts left out of the scale: switched off (4) and cooling (5).
SCALE_SKIP_CONTEXTS = (4, 5)
# HAC1's summer lock: the valve stays shut, so these minutes say nothing about it.
SUMMER_OUTDOOR_C = 15.0
SCALE_FIELDS = ("afterheat_level", "heating_coil_after_temperature", "afterheat_before_temperature",
                "outdoor_temp", "extract_temp", "exhaust_temp", "fan_supply_percent",
                "return_temperature", "flow_temperature", "afterheat_target", "afterheat_context")
MAX_FAN_GROUPS = 6
HEALTH_FIELDS = ("heating_coil_after_temperature", "afterheat_target", "afterheat_active",
                 "afterheat_level", "fan_supply_percent", "afterheat_context")
HEALTH_DAYS = 7
IN_BAND_K = 0.5
# Sensors with a freshness marker in the history and their Danish names.
STALE_SENSORS = {
    "supply_temp": "Indblæsning (T2AH)",
    "heating_coil_frost_temperature": "Frostføler",
    "outdoor_temp": "Ude (T1)",
    "extract_temp": "Udsugning (T3)",
    "exhaust_temp": "Afkast (T4)",
    "humidity": "Fugt",
    "co2": "CO₂",
}


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _mean(values: list[float]) -> float | None:
    return round(statistics.fmean(values), 2) if values else None


def before_coil(row: dict) -> tuple[float | None, str | None]:
    """Air before the coil: the 1-Wire T2 when recorded, else estimated from T1, T3 and T4."""
    measured = _number(row.get("afterheat_before_temperature"))
    if measured is not None:
        return measured, "measured"
    t1, t3, t4 = (_number(row.get(key)) for key in ("outdoor_temp", "extract_temp", "exhaust_temp"))
    if t1 is None or t3 is None or t4 is None or abs(t3 - t1) < 0.5:
        return None, None
    recovery = (t3 - t4) / (t3 - t1) * 100
    if not 0 <= recovery <= 105:
        return None, None
    return supply_after_core(t1, t3, recovery, False), "estimated"


def heat_scale(fan: float, water: float | None, target: float | None) -> float:
    """Heat per level against the pilot's reference (as AfterheatPilot._scale, unfiltered)."""
    scale = FAN_REFERENCE_PERCENT / fan
    if water is not None and target is not None and 0 < water < 95 and water - target >= WATER_MIN_LIFT_K:
        scale *= (water - target) / WATER_REFERENCE_LIFT_K
    return scale


def _settled(rows: list[dict]):
    """Rows on a level held SETTLE_MINUTES samples in a row at the same fan."""
    run, previous = 0, None
    for row in rows:
        level, fan, ts = _number(row.get("afterheat_level")), _number(row.get("fan_supply_percent")), _number(row.get("ts"))
        context = row.get("afterheat_context")
        outdoor = _number(row.get("outdoor_temp"))
        usable = (level is not None and fan is not None and fan > 0 and ts is not None
                  and _number(row.get("heating_coil_after_temperature")) is not None
                  and context not in SCALE_SKIP_CONTEXTS and (outdoor is None or outdoor < SUMMER_OUTDOOR_C))
        if not usable:
            run, previous = 0, None
            continue
        if (previous is not None and 0 < ts - previous[0] <= SAMPLE_GAP_S
                and previous[1] == level and previous[2] == round(fan)):
            run += 1
        else:
            run = 1
        previous = (ts, level, round(fan))
        if run >= SETTLE_MINUTES:
            yield row, int(round(level)), round(fan)


def _steps(lifts: dict[int, float]) -> dict[int, float]:
    """K per level step, from each measured level to the next lower measured one."""
    steps, below = {}, None
    for level in sorted(lifts):
        if below is not None:
            steps[level] = round((lifts[level] - lifts[below]) / (level - below), 2)
        below = level
    return steps


def afterheat_scale(rows: list[dict], hours: float) -> dict:
    cells: dict[tuple[int, int], dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    reference: dict[int, list[float]] = defaultdict(list)
    sources: set[str] = set()
    for row, level, fan in _settled(rows):
        if not 0 <= level <= LEVEL_MAX:
            continue
        cell = cells[(fan, level)]
        air = float(row["heating_coil_after_temperature"])
        cell["air"].append(air)
        before, source = before_coil(row)
        if before is not None:
            sources.add(source)
            cell["lift"].append(air - before)
            water, target = _number(row.get("flow_temperature")), _number(row.get("afterheat_target"))
            reference[level].append((air - before) / heat_scale(fan, water, target))
        for key in ("return_temperature", "flow_temperature"):
            value = _number(row.get(key))
            if value is not None:
                cell[key].append(value)
    fans = []
    for fan in sorted({fan for fan, _level in cells}):
        levels, lifts = [], {}
        for level in range(LEVEL_MAX + 1):
            cell = cells.get((fan, level))
            minutes = len(cell["air"]) if cell else 0
            enough = minutes >= MIN_POINTS
            lift = _mean(cell["lift"]) if enough and len(cell["lift"]) >= MIN_POINTS else None
            if lift is not None:
                lifts[level] = lift
            levels.append({
                "level": level, "valve_percent": level * 10, "minutes": minutes,
                "air": _mean(cell["air"]) if enough else None, "lift": lift,
                "return_temperature": _mean(cell["return_temperature"]) if enough else None,
                "flow_temperature": _mean(cell["flow_temperature"]) if enough else None,
                "step_k": None})
        for level, step in _steps(lifts).items():
            levels[level]["step_k"] = step
        fans.append({"fan_percent": fan, "minutes": sum(item["minutes"] for item in levels), "levels": levels})
    fans.sort(key=lambda item: -item["minutes"])
    reference_heat = {level: statistics.fmean(values) for level, values in reference.items() if len(values) >= MIN_POINTS}
    reference_steps: list[float | None] = [None] * LEVEL_MAX
    for level, step in _steps(reference_heat).items():
        below = max(other for other in reference_heat if other < level)
        for index in range(below, level):
            reference_steps[index] = step
    return {
        "hours": hours, "settle_minutes": SETTLE_MINUTES, "min_points": MIN_POINTS,
        "before_source": ("mixed" if len(sources) > 1 else next(iter(sources), None)),
        "fans": fans[:MAX_FAN_GROUPS],
        "reference_fan_percent": FAN_REFERENCE_PERCENT,
        "reference_steps": reference_steps,
    }


def _day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def _hour(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H", time.localtime(ts))


class _Period:
    def __init__(self) -> None:
        self.samples = 0
        self.gap_minutes = 0.0
        self.errors: list[float] = []
        self.air: list[float] = []
        self.targets: list[float] = []
        self.level_changes = 0
        self.fan_changes = 0
        self.covered_s = 0.0
        self.hour_changes: dict[str, int] = defaultdict(int)
        self.stale: dict[str, int] = defaultdict(int)

    def result(self) -> dict:
        errors = self.errors
        hours = self.covered_s / 3600
        return {
            "samples": self.samples,
            "gap_minutes": round(self.gap_minutes),
            "supply_minutes": len(errors),
            "target_mean": _mean(self.targets),
            "air_min": round(min(self.air), 2) if self.air else None,
            "air_max": round(max(self.air), 2) if self.air else None,
            "error_mean": _mean(errors),
            "error_sd": round(statistics.pstdev(errors), 2) if len(errors) >= 2 else None,
            "max_over": round(max(errors), 2) if errors else None,
            "max_under": round(-min(errors), 2) if errors else None,
            "in_band_percent": round(100 * sum(abs(e) <= IN_BAND_K for e in errors) / len(errors), 1) if errors else None,
            "level_changes": self.level_changes,
            "level_changes_per_hour": round(self.level_changes / hours, 2) if hours >= 0.5 else None,
            "level_changes_max_hour": max(self.hour_changes.values(), default=0),
            "fan_changes": self.fan_changes,
            "stale_minutes": {key: self.stale.get(key, 0) for key in STALE_SENSORS},
        }


def weekly_health(rows: list[dict], *, events: list[dict], restarts: list[dict], controller: dict,
                  now: float | None = None, days: int = HEALTH_DAYS) -> dict:
    now = time.time() if now is None else now
    since = now - days * 86400
    total, per_day = _Period(), defaultdict(_Period)
    previous = None
    for row in rows:
        ts = _number(row.get("ts"))
        if ts is None or ts < since:
            continue
        periods = (total, per_day[_day(ts)])
        for period in periods:
            period.samples += 1
            for key in STALE_SENSORS:
                if row.get(f"{key}_sample_fresh") == 0:
                    period.stale[key] += 1
        air, target = _number(row.get("heating_coil_after_temperature")), _number(row.get("afterheat_target"))
        active = _number(row.get("afterheat_active"))
        if air is not None and target is not None and active is not None and active > 0 and row.get("afterheat_context") not in (4, 5):
            for period in periods:
                period.errors.append(air - target)
                period.air.append(air)
                period.targets.append(target)
        if previous is not None:
            gap = ts - previous["_ts"]
            if gap > 3 * SAMPLE_GAP_S:
                for period in periods:
                    period.gap_minutes += gap / 60
            elif 0 < gap <= SAMPLE_GAP_S:
                level, before = _number(row.get("afterheat_level")), _number(previous.get("afterheat_level"))
                fan, fan_before = _number(row.get("fan_supply_percent")), _number(previous.get("fan_supply_percent"))
                if level is not None and before is not None:
                    for period in periods:
                        period.covered_s += gap
                        if round(level) != round(before):
                            period.level_changes += 1
                            period.hour_changes[_hour(ts)] += 1
                if fan is not None and fan_before is not None and round(fan) != round(fan_before):
                    for period in periods:
                        period.fan_changes += 1
        previous = {**row, "_ts": ts}
    command_alarms = [e for e in events if e.get("kind") == "alarm_raised" and e.get("code") == "controller_command_failed"
                      and (_number(e.get("time")) or 0) >= since]
    stale_alarms = [e for e in events if e.get("kind") == "alarm_raised" and e.get("code") == "local_sensor_stale"
                    and (_number(e.get("time")) or 0) >= since]
    week_restarts = [r for r in restarts if (_number(r.get("at")) or 0) >= since]
    pending = controller.get("pending_commands") if isinstance(controller.get("pending_commands"), dict) else {}
    sensors = ((controller.get("data_health") or {}).get("sensors") or {}) if isinstance(controller.get("data_health"), dict) else {}
    return {
        "generated_at": round(now), "since": round(since), "days": days,
        "total": total.result(),
        "per_day": [{"date": day, **period.result()} for day, period in sorted(per_day.items())],
        "command_errors": {
            "alarms": len(command_alarms),
            "recent": [{"time": e.get("time"), "text": e.get("text")} for e in command_alarms[-10:]],
            "pending_now": sorted(pending),
        },
        "watchdog_restarts": {
            "count": len(week_restarts),
            "recent": [{"at": r.get("at"), "reason": r.get("reason")} for r in week_restarts[-10:]],
        },
        "stale": {
            "sensors": STALE_SENSORS,
            "alarms": len(stale_alarms),
            "now": sorted(key for key, item in sensors.items() if isinstance(item, dict) and item.get("fresh") is False),
        },
        "note": ("Beregnet på gemte målinger én gang i minuttet; korte trinskift mellem to målinger tælles ikke. "
                 "Indblæsning sammenlignes med eftervarmens mål, mens eftervarmen er aktiv (ikke slukket eller køling). "
                 "For gamle målinger tælles fra opdateringen, hvor kvalitetsmarkørerne blev gemt."),
    }
