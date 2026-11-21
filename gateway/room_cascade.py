"""Afterheat supply target from the rooms (outer loop of a cascade).

The Pi's afterheat control (afterheat_pilot, the inner loop) holds the
supply air at a target. This slow outer PI moves the temperature the supply
air should reach the rooms with between ``afterheat_room_min`` and
``afterheat_room_max`` so the average of the rooms reaches
``afterheat_room_target``. The caller adds the duct loss offset
(``afterheat_room_offset_k``) to get the setpoint at the unit.

Velocity form, once per ``afterheat_room_step_minutes``:

    change = gain * (e - e_previous) + gain * step / integral * e,  e = target - rooms

limited to ``afterheat_room_max_step`` K per step; the output is the exact
value rounded to 0.1 K (small integral steps add up instead of being rounded
away). Inside the dead band (``afterheat_room_band_k`` around the room
target) the target is left alone. Because only the change is computed and
the output is clamped, nothing winds up: at min/max, or while the afterheat
cannot follow (HAC1 at full output and still too cold, or off and still too
warm), steps that would push further into the limit are dropped. While the
afterheat is locked (frost lock above the target, Køl or night cooling,
afterheat off, a test override) the target is frozen.

Kept free of the controller so the rules are easy to read and test.
"""
from __future__ import annotations

# The supply air counts as unable to follow when it misses its target by this much.
SATURATION_MARGIN_K = 0.5


def _round(value: float) -> float:
    return round(value * 10) / 10


class RoomCascade:
    def __init__(self, start: float | None = None) -> None:
        self.exact: float | None = start
        self.value: float | None = None if start is None else _round(start)
        self.last_step_at: float | None = None
        self.last_error: float | None = None
        self.state = "disabled"

    def reset(self) -> None:
        self.exact, self.value, self.last_step_at, self.last_error = None, None, None, None

    def update(self, *, now: float, room: float | None, settings: dict, base: float,
               locked: str | None = None, saturated: str | None = None) -> float | None:
        """The wanted supply temperature at the rooms, or None when the fixed setpoint must be used.

        ``saturated`` is "high" (afterheat at full output and the supply air
        still too cold) or "low" (afterheat off and the supply still too warm).
        """
        d = settings
        low, high = float(d["afterheat_room_min"]), float(d["afterheat_room_max"])
        if room is None:
            self.state = "no_room_temperature"
            self.last_error = None
            return None
        if self.exact is None:
            self.exact = min(high, max(low, float(base)))
            self.last_step_at, self.last_error = now, None
            self.state = "holding"
        self.exact = min(high, max(low, self.exact))
        self.value = _round(self.exact)
        error = float(d["afterheat_room_target"]) - room
        band = max(0.0, float(d.get("afterheat_room_band_k", 0.3)))
        if locked:
            self.state = "locked"
            self.last_step_at, self.last_error = now, error
            return self.value
        interval = float(d["afterheat_room_step_minutes"]) * 60
        if self.last_step_at is not None and now - self.last_step_at < interval:
            return self.value
        previous = error if self.last_error is None else self.last_error
        self.last_step_at, self.last_error = now, error
        if abs(error) <= band:
            self.state = "in_band"
            return self.value
        gain = float(d["afterheat_room_gain"])
        integral = max(1.0, float(d["afterheat_room_integral_minutes"]))
        step_minutes = float(d["afterheat_room_step_minutes"])
        change = gain * (error - previous) + gain * step_minutes / integral * error
        limit = float(d["afterheat_room_max_step"])
        change = max(-limit, min(limit, change))
        if (saturated == "high" and change > 0) or (saturated == "low" and change < 0):
            self.state = "saturated"
            return self.value
        before = self.exact
        self.exact = min(high, max(low, self.exact + change))
        self.value = _round(self.exact)
        if (self.exact >= high and error > 0) or (self.exact <= low and error < 0):
            self.state = "at_limit"
        elif self.exact > before + 1e-9:
            self.state = "rising"
        elif self.exact < before - 1e-9:
            self.state = "falling"
        else:
            self.state = "holding"
        return self.value


__all__ = ["RoomCascade", "SATURATION_MARGIN_K"]
