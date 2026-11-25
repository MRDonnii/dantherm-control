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
                 "afterheat_level", "fan_supply_percent", "afterheat_context", "afterheat_algorithm_code")
HEALTH_DAYS = 7
IN_BAND_K = 0.5
# The combined HA-room status that the weekly report repeats.
HA_ROOMS_KEYS = ("state", "fresh", "connection_online", "fresh_count", "stale_count", "unknown_age_count",
                 "missing_count", "missing_rooms_count")
# Operating-mode filter of the afterheat precision (operating_context.afterheat_context values,
# as the History page's "Vurder driftstilstand"); "unknown" = older or mixed data.
HEALTH_CONTEXTS = ("all", *(str(value) for value in range(9)), "unknown",
                   # Or which afterheat algorithm steered (afterheat_control.ALGORITHMS).
                   "algo:standard", "algo:mpc", "algo:mpc_simple", "algo:pid", "algo:tpi", "algo:heat_power")
ALGORITHM_CONTEXT_CODES = {"algo:standard": 0, "algo:mpc": 1, "algo:mpc_simple": 2, "algo:pid": 3, "algo:tpi": 4,
                           "algo:heat_power": 5}
# What the report says when its data basis is missing or recording is paused.
DATA_BASIS_TEXT = {
    "ok": "",
    "paused": "Historikken optager ikke – tallene mangler data",
    "unreadable": "Historikken kunne ikke læses – tallene mangler data",
    "missing": "Ingen gemte målinger i perioden – tallene mangler data",
}
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


def _next_midnight(ts: float) -> float:
    day = time.localtime(ts)
    return time.mktime((day.tm_year, day.tm_mon, day.tm_mday + 1, 0, 0, 0, 0, 0, -1))


def _add_gap(total: "_Period", per_day: dict, start: float, end: float) -> None:
    """A gap from ``start`` to ``end`` counts in full in the total and is split over the days it spans."""
    total.gap_minutes += (end - start) / 60
    while start < end:
        stop = min(end, max(_next_midnight(start), start + 1))
        per_day[_day(start)].gap_minutes += (stop - start) / 60
        start = stop


def row_context(row: dict) -> str:
    """The recorded operating mode of one sample as a HEALTH_CONTEXTS key."""
    value = _number(row.get("afterheat_context"))
    return str(int(value)) if value is not None and value.is_integer() and 0 <= value <= 8 else "unknown"


def row_matches(row: dict, context: str) -> bool:
    """The sample belongs to ``context``: all, its operating mode or the algorithm that steered."""
    if context == "all":
        return True
    if context in ALGORITHM_CONTEXT_CODES:
        return _number(row.get("afterheat_algorithm_code")) == ALGORITHM_CONTEXT_CODES[context]
    return row_context(row) == context


class _Period:
    def __init__(self) -> None:
        self.samples = 0
        self.context_samples = 0
        self.gap_minutes = 0.0
        self.errors: list[float] = []
        self.air: list[float] = []
        self.targets: list[float] = []
        self.level_changes = 0
        self.fan_changes = 0
        self.covered_s = 0.0
        self.hour_changes: dict[str, int] = defaultdict(int)
        self.stale: dict[str, int] = defaultdict(int)
        self.outdoor: list[float] = []

    def result(self) -> dict:
        errors = self.errors
        hours = self.covered_s / 3600
        return {
            "samples": self.samples,
            "context_minutes": self.context_samples,
            "gap_minutes": round(self.gap_minutes),
            "supply_minutes": len(errors),
            "target_mean": _mean(self.targets),
            "air_min": round(min(self.air), 2) if self.air else None,
            "air_max": round(max(self.air), 2) if self.air else None,
            "error_mean": _mean(errors),
            "error_sd": round(statistics.pstdev(errors), 2) if len(errors) >= 2 else None,
            "max_over": round(max(0.0, max(errors)), 2) if errors else None,
            "max_under": round(max(0.0, -min(errors)), 2) if errors else None,
            "in_band_percent": round(100 * sum(abs(e) <= IN_BAND_K for e in errors) / len(errors), 1) if errors else None,
            "level_changes": self.level_changes,
            "level_changes_per_hour": round(self.level_changes / hours, 2) if hours >= 0.5 else None,
            "level_changes_max_hour": max(self.hour_changes.values(), default=0),
            "fan_changes": self.fan_changes,
            "stale_minutes": {key: self.stale.get(key, 0) for key in STALE_SENSORS},
        }


def _scan(rows: list[dict], *, since: float, until: float, context: str, total: "_Period",
          per_day: dict | None = None) -> float | None:
    """Count the samples in [since, until] into ``total`` (and ``per_day``); the last sample time.

    ``context`` filters the afterheat precision and the level changes; samples, gaps,
    fan changes and stale minutes are never filtered."""
    previous = None
    for row in rows:
        ts = _number(row.get("ts"))
        if ts is None or ts < since or ts > until:
            continue
        periods = (total, per_day[_day(ts)]) if per_day is not None else (total,)
        selected = row_matches(row, context)
        for period in periods:
            period.samples += 1
            period.context_samples += selected
            for key in STALE_SENSORS:
                if row.get(f"{key}_sample_fresh") == 0:
                    period.stale[key] += 1
        if selected:
            outdoor = _number(row.get("outdoor_temp"))
            if outdoor is not None:
                total.outdoor.append(outdoor)
        air, target = _number(row.get("heating_coil_after_temperature")), _number(row.get("afterheat_target"))
        active = _number(row.get("afterheat_active"))
        if (selected and air is not None and target is not None and active is not None and active > 0
                and row.get("afterheat_context") not in (4, 5)):
            for period in periods:
                period.errors.append(air - target)
                period.air.append(air)
                period.targets.append(target)
        if previous is not None:
            gap = ts - previous["_ts"]
            if gap > 3 * SAMPLE_GAP_S:
                if per_day is not None:
                    _add_gap(total, per_day, previous["_ts"], ts)
                else:
                    total.gap_minutes += gap / 60
            elif 0 < gap <= SAMPLE_GAP_S:
                level, before = _number(row.get("afterheat_level")), _number(previous.get("afterheat_level"))
                fan, fan_before = _number(row.get("fan_supply_percent")), _number(previous.get("fan_supply_percent"))
                both_selected = selected and row_matches(previous, context)
                if level is not None and before is not None and both_selected:
                    for period in periods:
                        period.covered_s += gap
                        if round(level) != round(before):
                            period.level_changes += 1
                            period.hour_changes[_hour(ts)] += 1
                if fan is not None and fan_before is not None and round(fan) != round(fan_before):
                    for period in periods:
                        period.fan_changes += 1
        previous = {**row, "_ts": ts}
    return previous["_ts"] if previous is not None else None


def weekly_health(rows: list[dict], *, events: list[dict], restarts: list[dict], controller: dict,
                  now: float | None = None, days: int = HEALTH_DAYS, context: str = "all",
                  history: dict | None = None) -> dict:
    """``context`` filters the afterheat precision (spread, min/max, in band, over/undershoot,
    level changes) by operating mode; samples, gaps, fan changes, stale minutes and alarms
    are never filtered. ``history`` is HistoryStore.status() for the data basis."""
    now = time.time() if now is None else now
    context = context if context in HEALTH_CONTEXTS else "all"
    since = now - days * 86400
    total, per_day = _Period(), defaultdict(_Period)
    last_ts = _scan(rows, since=since, until=math.inf, context=context, total=total, per_day=per_day)
    # A gap still open now (no sample for a while) counts too, split over the days it spans.
    open_gap_s = now - last_ts if last_ts is not None else 0.0
    if open_gap_s > 3 * SAMPLE_GAP_S:
        _add_gap(total, per_day, last_ts, now)
    else:
        open_gap_s = 0.0
    recording = history if isinstance(history, dict) else {}
    if recording.get("read_error"):
        basis = "unreadable"
    elif recording.get("available") is False:
        basis = "paused"
    elif not total.samples:
        basis = "missing"
    else:
        basis = "ok"
    command_alarms = [e for e in events if e.get("kind") == "alarm_raised" and e.get("code") == "controller_command_failed"
                      and (_number(e.get("time")) or 0) >= since]
    stale_alarms = [e for e in events if e.get("kind") == "alarm_raised" and e.get("code") == "local_sensor_stale"
                    and (_number(e.get("time")) or 0) >= since]
    week_restarts = [r for r in restarts if (_number(r.get("at")) or 0) >= since]
    pending = controller.get("pending_commands") if isinstance(controller.get("pending_commands"), dict) else {}
    sensors = ((controller.get("data_health") or {}).get("sensors") or {}) if isinstance(controller.get("data_health"), dict) else {}
    return {
        "generated_at": round(now), "since": round(since), "days": days,
        "context": context,
        # Without stored samples (or with recording paused) the numbers below are not a week:
        # 0 gaps then means "unknown", not "no gaps".
        "data_basis": {
            "state": basis, "ok": basis == "ok", "text": DATA_BASIS_TEXT[basis],
            "gaps_known": total.samples > 0 and basis in ("ok", "paused"),
            "recording": recording.get("state"), "next_retry_at": recording.get("next_retry_at"),
            "error": recording.get("error") or recording.get("read_error"),
        },
        "total": total.result(),
        "last_sample_age_minutes": round((now - last_ts) / 60, 1) if last_ts is not None else None,
        "open_gap_minutes": round(open_gap_s / 60),
        "per_day": [{"date": day, **period.result()} for day, period in sorted(per_day.items())],
        "command_errors": {
            "alarms": len(command_alarms),
            # The ten newest, newest first (whatever order the event log hands them in).
            "recent": [{"time": e.get("time"), "text": e.get("text")}
                       for e in sorted(command_alarms, key=lambda e: _number(e.get("time")) or 0, reverse=True)[:10]],
            "pending_now": sorted(pending),
        },
        "watchdog_restarts": {
            "count": len(week_restarts),
            "recent": [{"at": r.get("at"), "reason": r.get("reason")}
                       for r in sorted(week_restarts, key=lambda r: _number(r.get("at")) or 0, reverse=True)[:10]],
        },
        "stale": {
            "sensors": STALE_SENSORS,
            "alarms": len(stale_alarms),
            # "Too old now"; HA rooms whose only problem is a missing sensor are shown in ha_rooms instead.
            "now": sorted(key for key, item in sensors.items() if isinstance(item, dict) and item.get("fresh") is False
                          and not (key == "ha_rooms" and item.get("state") == "missing")),
            # Connection and measurement age are separate; unknown age is not "too old".
            "ha_rooms": {key: sensors["ha_rooms"].get(key) for key in HA_ROOMS_KEYS}
            if isinstance(sensors.get("ha_rooms"), dict) and "state" in sensors["ha_rooms"] else None,
        },
        "note": ("Beregnet på gemte målinger én gang i minuttet; korte trinskift mellem to målinger tælles ikke. "
                 "Indblæsning sammenlignes med eftervarmens mål, mens eftervarmen er aktiv (ikke slukket eller køling). "
                 "For gamle målinger tælles fra opdateringen, hvor kvalitetsmarkørerne blev gemt."),
    }


# --- Before/after a software update -------------------------------------------------------

COMPARE_FIELDS = ("heating_coil_after_temperature", "afterheat_target", "afterheat_active", "afterheat_level",
                  "fan_supply_percent", "afterheat_context", "outdoor_temp", "afterheat_algorithm_code")
COMPARE_WINDOWS = {"24h": 86400, "3d": 3 * 86400, "7d": 7 * 86400}
# Start-up and settling after an update are left out of both windows.
COMPARE_SETTLE_S = 30 * 60
# Fewer minutes than this measured against the target on a side: no verdict.
COMPARE_MIN_MINUTES = 6 * 60
COMPARE_LABELS = {"better": "Bedre", "worse": "Dårligere", "unchanged": "Uændret",
                  "too_little": "For lidt data", "none": "–"}
# key, Danish label, unit, which way is better (None = not judged), noise threshold, relative threshold.
COMPARE_METRICS = (
    ("error_sd", "Spredning (standardafvigelse)", "K", "lower", 0.1, 0.0),
    ("error_mean", "Gennemsnitlig afvigelse", "K", "closer_to_zero", 0.1, 0.0),
    ("in_band_percent", "Tid inden for ±0,5 °C", "%", "higher", 5.0, 0.0),
    ("max_over", "Største oversving", "K", "lower", 0.3, 0.0),
    ("max_under", "Største undersving", "K", "lower", 0.3, 0.0),
    ("level_changes_per_hour", "Eftervarme: trinskift pr. time", "", "lower", 0.5, 0.2),
    ("fan_changes_per_hour", "Blæser: trinskift pr. time", "", None, 0.0, 0.0),
    ("command_errors", "Alarmer for fejlede kommandoer", "", "lower_count", 1.0, 0.0),
    ("watchdog_restarts", "Genstarter fra vagthunden", "", "lower_count", 1.0, 0.0),
)


def comparison_windows(updates: list[dict], index: int, window_s: float, now: float) -> dict:
    """The before and after windows of ``updates[index]`` (oldest-first list).

    Before: up to ``window_s`` ending at the update, starting no earlier than the settled
    start of the previous version. After: from the settled start, up to ``window_s``, ending
    no later than the next update or now. So the two never mix versions."""
    at = float(updates[index]["at"])
    previous_at = float(updates[index - 1]["at"]) if index > 0 else None
    next_at = float(updates[index + 1]["at"]) if index + 1 < len(updates) else None
    before_start = at - window_s
    if previous_at is not None:
        before_start = max(before_start, previous_at + COMPARE_SETTLE_S)
    after_start = at + COMPARE_SETTLE_S
    after_end = after_start + window_s
    limits = [after_end, now]
    if next_at is not None:
        limits.append(next_at)
    after_end = min(limits)
    return {
        "before": {"start": round(min(before_start, at)), "end": round(at),
                   "cut_by_previous_update": previous_at is not None and previous_at + COMPARE_SETTLE_S > at - window_s},
        "after": {"start": round(after_start), "end": round(max(after_start, after_end)),
                  "cut_by_next_update": next_at is not None and next_at < min(after_start + window_s, now),
                  "ongoing": next_at is None and now < after_start + window_s},
    }


def _window_side(rows: list[dict], start: float, end: float, context: str, events: list[dict],
                 events_known_since: float, restarts: list[dict], restarts_known_since: float) -> dict:
    period = _Period()
    first_ts = None
    for row in rows:
        ts = _number(row.get("ts"))
        if ts is not None and start <= ts < end:
            first_ts = ts
            break
    # Half-open windows: a sample at the update/next update belongs to the new version.
    last_ts = _scan(rows, since=start, until=math.nextafter(end, -math.inf), context=context, total=period)
    window_minutes = max(0.0, (end - start) / 60)
    # A missing start or end of the window is a gap too.
    if first_ts is None:
        period.gap_minutes += window_minutes
    else:
        if first_ts - start > 3 * SAMPLE_GAP_S:
            period.gap_minutes += (first_ts - start) / 60
        if last_ts is not None and end - last_ts > 3 * SAMPLE_GAP_S:
            period.gap_minutes += (end - last_ts) / 60
    result = period.result()
    hours = window_minutes / 60
    sample_hours = period.samples / 60
    errors = [e for e in events if e.get("kind") == "alarm_raised" and e.get("code") == "controller_command_failed"
              and start <= (_number(e.get("time")) or 0) <= end]
    window_restarts = [r for r in restarts if start <= (_number(r.get("at")) or 0) <= end]
    outdoor = period.outdoor
    return {
        "start": round(start), "end": round(end), "window_minutes": round(window_minutes),
        "samples": result["samples"], "context_minutes": result["context_minutes"],
        "supply_minutes": result["supply_minutes"], "gap_minutes": round(period.gap_minutes),
        "coverage_percent": min(100.0, round(100 * result["context_minutes"] / window_minutes, 1)) if window_minutes >= 1 else 0.0,
        "enough": result["supply_minutes"] >= COMPARE_MIN_MINUTES,
        "outdoor_min": round(min(outdoor), 1) if outdoor else None,
        "outdoor_mean": round(statistics.fmean(outdoor), 1) if outdoor else None,
        "outdoor_max": round(max(outdoor), 1) if outdoor else None,
        "target_mean": result["target_mean"],
        "error_sd": result["error_sd"], "error_mean": result["error_mean"],
        "in_band_percent": result["in_band_percent"],
        "max_over": result["max_over"], "max_under": result["max_under"],
        "level_changes": result["level_changes"], "level_changes_per_hour": result["level_changes_per_hour"],
        "fan_changes": result["fan_changes"],
        "fan_changes_per_hour": round(result["fan_changes"] / sample_hours, 2) if sample_hours >= 0.5 else None,
        # The alarm log keeps the last 500 events and restarts are kept 8 days: older is unknown.
        "command_errors": len(errors) if start >= events_known_since else None,
        "watchdog_restarts": len(window_restarts) if start >= restarts_known_since else None,
        "hours": round(hours, 2),
    }


def _judge(key: str, direction: str | None, threshold: float, relative: float, before: dict, after: dict,
           enough: bool) -> str:
    a, b = before.get(key), after.get(key)
    if a is None or b is None or direction is None:
        return "none"
    if not enough:
        return "too_little"
    if direction == "lower_count":
        # Events are counted, so a shorter window expects fewer: compare with the before rate.
        hours_a, hours_b = before.get("hours") or 0, after.get("hours") or 0
        expected = a * hours_b / hours_a if hours_a > 0 else a
        if b >= expected + threshold:
            return "worse"
        if b <= expected - threshold:
            return "better"
        return "unchanged"
    if direction == "closer_to_zero":
        a, b, direction = abs(a), abs(b), "lower"
    limit = max(threshold, relative * abs(a))
    delta = b - a
    if abs(delta) < limit or delta == 0:
        return "unchanged"
    improved = delta < 0 if direction == "lower" else delta > 0
    return "better" if improved else "worse"


def update_comparison(update: dict, windows: dict, before_rows: list[dict], after_rows: list[dict], *,
                      context: str = "all", window: str = "24h", events: list[dict] | None = None,
                      events_known_since: float = -math.inf, restarts: list[dict] | None = None,
                      restarts_known_since: float = -math.inf) -> dict:
    """Regulation quality in the windows before and after one update (read-only).

    The afterheat precision uses the weekly report's rules and its ``context`` filter.
    No verdict when a side has under COMPARE_MIN_MINUTES measured against the target."""
    context = context if context in HEALTH_CONTEXTS else "all"
    events, restarts = events or [], restarts or []
    sides = {}
    for name, rows in (("before", before_rows), ("after", after_rows)):
        span = windows[name]
        sides[name] = {**_window_side(rows, span["start"], span["end"], context, events, events_known_since,
                                      restarts, restarts_known_since),
                       **{key: value for key, value in span.items() if key not in ("start", "end")}}
    before, after = sides["before"], sides["after"]
    enough = before["enough"] and after["enough"]
    metrics, counts = [], {"better": 0, "worse": 0, "unchanged": 0}
    for key, label, unit, direction, threshold, relative in COMPARE_METRICS:
        verdict = _judge(key, direction, threshold, relative, before, after, enough)
        if verdict in counts:
            counts[verdict] += 1
        a, b = before.get(key), after.get(key)
        metrics.append({
            "key": key, "label": label, "unit": unit, "before": a, "after": b,
            "delta": round(b - a, 2) if a is not None and b is not None else None,
            "judged": direction is not None, "threshold": threshold, "relative_threshold": relative,
            "verdict": verdict, "verdict_text": COMPARE_LABELS[verdict],
        })
    if not enough:
        short = [text for side, text in (("before", "før"), ("after", "efter")) if not sides[side]["enough"]]
        summary = (f"For lidt data {' og '.join(short)} opdateringen til at sammenligne: under "
                   f"{COMPARE_MIN_MINUTES // 60} timer med indblæsning mod målet i den valgte driftstilstand.")
    else:
        summary = f"{counts['better']} bedre · {counts['worse']} dårligere · {counts['unchanged']} uændret"
    return {
        "update": update, "window": window, "window_seconds": COMPARE_WINDOWS.get(window),
        "context": context, "settle_minutes": COMPARE_SETTLE_S // 60, "min_minutes": COMPARE_MIN_MINUTES,
        "enough": enough, "before": before, "after": after, "metrics": metrics, "counts": counts,
        "summary": summary,
        "note": ("Vejr og brug er ikke ens i de to perioder: se udetemperaturen og dækningen. "
                 f"De første {COMPARE_SETTLE_S // 60} min efter opdateringen tælles ikke med, og perioden før "
                 "stopper ved den forrige opdatering, så to versioner aldrig blandes. "
                 "Forskelle under tærsklen kaldes uændret."),
    }


# --- Per afterheat algorithm (afterheat_control.ALGORITHMS) ------------------------------

ALGORITHM_FIELDS = ("heating_coil_after_temperature", "afterheat_target", "afterheat_level",
                    "afterheat_algorithm_code", "afterheat_safety_override", "afterheat_context")
ALGORITHM_WINDOWS = {"1h": 3600, "24h": 86400, "7d": 7 * 86400}
# Fewer measured minutes than this for an algorithm in a window: shown, but not compared.
ALGORITHM_MIN_MINUTES = 30
SOFT_FLOOR_K = 0.5


def algorithm_performance(rows: list[dict], now: float) -> dict:
    """How each algorithm did in the last hour, day and week (read-only, from the one-minute history).

    Per window and algorithm code: measured minutes, mean deviation from the target, lowest air,
    share of minutes under target - 0.5 K, level changes an hour and minutes the safety layer
    took over. Minutes with HAC1 alone (no code), switched off or cooling are left out; a level
    change counts for the algorithm when both minutes are its own and one minute apart.
    """
    from afterheat_control import ALGORITHM_CODES, ALGORITHM_LABELS
    names = {code: name for name, code in ALGORITHM_CODES.items()}
    result = {}
    for window, span in ALGORITHM_WINDOWS.items():
        since = now - span
        stats: dict[str, dict] = {}
        previous = None
        for row in rows:
            ts = _number(row.get("ts"))
            if ts is None or ts < since or ts > now:
                continue
            code = _number(row.get("afterheat_algorithm_code"))
            name = names.get(int(code)) if code is not None and code.is_integer() else None
            air, target = _number(row.get("heating_coil_after_temperature")), _number(row.get("afterheat_target"))
            level = _number(row.get("afterheat_level"))
            if name is not None and row.get("afterheat_context") not in (4, 5):
                item = stats.setdefault(name, {"minutes": 0, "errors": [], "air": [], "under": 0, "changes": 0,
                                               "covered_s": 0.0, "overrides": 0})
                item["minutes"] += 1
                if (_number(row.get("afterheat_safety_override")) or 0) > 0:
                    item["overrides"] += 1
                if air is not None and target is not None:
                    item["errors"].append(air - target)
                    item["air"].append(air)
                    item["under"] += air < target - SOFT_FLOOR_K
                if previous is not None and previous[1] == name and 0 < ts - previous[0] <= SAMPLE_GAP_S:
                    item["covered_s"] += ts - previous[0]
                    if level is not None and previous[2] is not None and round(level) != round(previous[2]):
                        item["changes"] += 1
            previous = (ts, name, level)
        listed = []
        for name, item in stats.items():
            errors, hours = item["errors"], item["covered_s"] / 3600
            listed.append({
                "algorithm": name, "label": ALGORITHM_LABELS.get(name, name), "minutes": item["minutes"],
                "measured_minutes": len(errors),
                "enough": len(errors) >= ALGORITHM_MIN_MINUTES,
                "mean_error": round(statistics.fmean(errors), 2) if errors else None,
                "mean_abs_error": round(statistics.fmean(abs(e) for e in errors), 2) if errors else None,
                "air_min": round(min(item["air"]), 1) if item["air"] else None,
                "under_floor_percent": round(100 * item["under"] / len(errors), 1) if errors else None,
                "level_changes_per_hour": round(item["changes"] / hours, 1) if hours >= 0.25 else None,
                "safety_minutes": item["overrides"],
            })
        listed.sort(key=lambda entry: ALGORITHM_CODES.get(entry["algorithm"], 99))
        result[window] = {"algorithms": listed,
                          "compare": sum(1 for entry in listed if entry["enough"]) >= 2}
    return result
