"""Freshness of HA measurements, independent of the transport heartbeat."""
from __future__ import annotations
import math

# A conservative limit also accommodates battery sensors reporting only changes.
# Kept equal to the existing temperature policy; all limits are shown in the UI.
MAX_AGE_SECONDS = {kind: 3 * 3600 for kind in ('temperature', 'humidity', 'co2', 'pm25')}


def number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def quality_rooms(rooms: dict, elapsed: float = 0, *, online: bool = True) -> tuple[dict, dict]:
    effective, quality = {}, {}
    for name, original in rooms.items():
        values, sensors = dict(original), {}
        strict = original.get('measurement_age_version') == 1
        for kind, limit in MAX_AGE_SECONDS.items():
            value = number(original.get(kind))
            age = number(original.get(f'{kind}_age_s'))
            if age is not None and age >= 0:
                age += max(0, elapsed)
            else:
                age = None
            state = ('unavailable' if value is None else 'connection_stale' if not online else
                     'unknown_age' if age is None else 'stale' if age > limit else 'fresh')
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
