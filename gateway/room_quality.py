"""Freshness of HA measurements, independent of the transport heartbeat."""
from __future__ import annotations
import math

# How old a value may be, per kind; all limits are shown in the UI. CO2 sensors
# report every few minutes, so a silent one must not hold a high level for hours.
# PM2.5 sensors often report only on change: in clean, steady air a value can be
# hours old. It counts while another value from the same room is fresh (the
# device is alive), otherwise up to 12 hours.
MAX_AGE_SECONDS = {'temperature': 3 * 3600, 'humidity': 3 * 3600, 'co2': 30 * 60, 'pm25': 12 * 3600}


def number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def quality_rooms(rooms: dict, elapsed: float = 0, *, online: bool = True) -> tuple[dict, dict]:
    effective, quality = {}, {}
    for name, original in rooms.items():
        values, sensors = dict(original), {}
        strict = original.get('measurement_age_version') == 1
        ages = {}
        for kind in MAX_AGE_SECONDS:
            age = number(original.get(f'{kind}_age_s'))
            ages[kind] = age + max(0, elapsed) if age is not None and age >= 0 else None
        companion_fresh = any(
            number(original.get(kind)) is not None and ages[kind] is not None and ages[kind] <= MAX_AGE_SECONDS[kind]
            for kind in MAX_AGE_SECONDS if kind != 'pm25')
        for kind, limit in MAX_AGE_SECONDS.items():
            value = number(original.get(kind))
            age = ages[kind]
            stale = age is not None and age > limit and not (kind == 'pm25' and companion_fresh)
            state = ('unavailable' if value is None else 'connection_stale' if not online else
                     'unknown_age' if age is None else 'stale' if stale else 'fresh')
            used = online and value is not None and (state == 'fresh' or (state == 'unknown_age' and not strict))
            sensors[kind] = {'state': state, 'age_seconds': round(age, 1) if age is not None else None,
                             'max_age_seconds': limit, 'used': used}
            if not used:
                values.pop(kind, None)
            if kind == 'temperature':
                values['temperature_stale'] = not used
        effective[name], quality[name] = values, sensors
    return effective, quality


def age_observations(rooms: dict, elapsed: float) -> dict:
    """Copy observations for a recording, with ages at recording time."""
    result = {}
    for name, original in rooms.items():
        values = dict(original)
        for kind in MAX_AGE_SECONDS:
            age = number(original.get(f'{kind}_age_s'))
            if age is not None and age >= 0:
                values[f'{kind}_age_s'] = age + max(0, elapsed)
        result[name] = values
    return result
