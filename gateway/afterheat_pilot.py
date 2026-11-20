"""Pi-side afterheat control on top of HAC1's own step controller.

HAC1 (Modbus slave 0x40) drives the water valve in whole levels 0..10
(register 101). Once per controller tick (a fixed clock, ~60.17 s) it moves
the level by at most one step, deciding on its own raw after-coil sensor
T2AH (register 205) against the WHOLE-DEGREE part of its setpoint (the
fraction of register 186 is ignored):

    T2AH < sp            -> one level up
    T2AH >= sp + ~0.95 K -> one level down
    otherwise            -> hold

So an integer setpoint sent shortly before a tick chooses what HAC1 does at
that tick (see ``hac1_setpoint``). This module keeps a continuous wanted
level from a PI controller on the filtered T2AH: a Smith predictor (dead
time + lag model of the coil, fitted to the 2026-11-20 experiments, with the
gain per level learned online) takes the coil's delay out of the loop, the
gain is scheduled by the supply fan (and the water flow temperature when a
1-Wire sensor has that role), with feed-forward on their changes,
anti-windup and a bumpless start from HAC1's level. A sigma-delta quantizer
turns the wanted level into whole levels and HAC1 is asked for at most one
step per tick. Everything here is pure logic: the caller passes the
measurements and the time, and sends the returned setpoint. Whenever the Pi
cannot do this safely, the result says so and the caller sends the plain
(native) setpoint instead, which is today's behaviour.
"""
from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass

LOG = logging.getLogger("afterheat-pilot")

CONTROL_MODES = ("native", "pi")
LEVEL_MAX = 10
# HAC1 controller tick, measured 2026-11-20 (drift ~0.17 s/min).
TICK_PERIOD_S = 60.17
# HAC1 register 101 is polled every ~3 s: a change is seen 0..3 s late.
OBSERVATION_LATENCY_S = 1.5
# A tick estimate older than this (no level change seen) is not trusted.
PHASE_VALID_S = 45 * 60
# An observed change further than this from the predicted tick re-anchors the clock.
PHASE_JUMP_S = 12.0
# The command for a tick is chosen this long before it ...
DECIDE_BEFORE_S = 15.0
# ... and the setpoint is left alone from this long before to this long after it.
FREEZE_S = 10.0
# Whether HAC1 followed is judged this long after the predicted tick.
CHECK_AFTER_S = 12.0
STALE_S = 30.0
FALLBACK_S = 30 * 60
FOLLOW_FAIL_LIMIT = 3
# A miss only counts when the air was at least this far inside the band at the decision.
FOLLOW_MARGIN_K = 0.15
RATE_WINDOW_S = 600.0
RATE_WARN_CHANGES = 8
# Above this many changes in the window only changes that stop a wrong step are made.
RATE_LIMIT_CHANGES = 6
QUANTIZER_HYSTERESIS = 0.15
# A step against the previous step waits this many ticks (no 1-tick dithering).
REVERSAL_DWELL_TICKS = 2
# ... unless the air is this far off the target.
REVERSAL_URGENT_K = 0.7
# The integral pauses while the wanted level is this far ahead of HAC1's level.
WINDUP_LEVELS = 1.5
# Integer setpoint mapping on the raw T2AH (see hac1_setpoint).
UP_OFFSET_K = 0.3
DOWN_OFFSET_K = 1.2
HOLD_FRACTION_MAX = 0.88
# The air is steered to this part of a degree (see hold_friendly).
HOLD_TARGET_BAND = (0.15, 0.78)
# Just above a whole degree, noise can make HAC1 step up instead of holding.
HOLD_FRACTION_MIN = 0.05
HAC1_DOWN_BAND_K = 0.95
# HAC1's own summer lockout (no afterheat at all); a known reason, not a fault.
SUMMER_OUTDOOR_C = 15.0
SUMMER_EXTRACT_C = 24.0
# Gain scheduling: heat per level is taken as proportional to 1/supply fan %.
FAN_REFERENCE_PERCENT = 27.0
FAN_STEP_PERCENT = 2.0
FEED_FORWARD = 1.0
# The 1-Wire water flow temperature (when there is one) scales the heat per
# level by its lift over the air; filtered, and only with a real lift.
WATER_FILTER_S = 120.0
WATER_MIN_LIFT_K = 8.0
WATER_REFERENCE_LIFT_K = 20.0
SETPOINT_MIN, SETPOINT_MAX = 10, 35
ROBUST_BAND_K = 0.3
# A raw reading this far above the filtered T2AH (15 s minimum) is a glitch,
# also when it lasts long enough to fill the raw window.
RAW_GLITCH_K = 0.5
# Trend of the raw T2AH used to predict it at the tick.
RAW_TREND_S = 30.0

# The PI works in "heat units" (level 1 = 1, concave like the throttled coil)
# through a dead time + lag model of the coil (fitted to the 2026-11-20 logs),
# so it judges the steady state of the level HAC1 already has (Smith predictor).
MODEL_DEAD_TIME_S = 60.0
MODEL_LAG_S = 120.0
MODEL_CURVATURE = 3.0
CLOSED_LOOP_S = 150.0
# Kelvin per heat unit at the reference fan, learned online with a prior.
GAIN_PRIOR, GAIN_PRIOR_WEIGHT, GAIN_RANGE = 1.0, 0.2, (0.3, 4.0)
GAIN_SAMPLE_S = 30.0
GAIN_MEMORY_S = 20 * 60.0
GAIN_FAN_SETTLE_S = 6 * 60.0
# afterheat_pi_gain scales the model-based gain; the integral time is in minutes.
PI_GAIN_DEFAULT, PI_GAIN_RANGE = 1.0, (0.3, 2.0)
PI_INTEGRAL_MINUTES_DEFAULT, PI_INTEGRAL_MINUTES_RANGE = 2.0, (1.0, 20.0)


def _num(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _danish(value: float, digits: int = 1) -> str:
    return f"{value:.{digits}f}".replace(".", ",")


def _clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def robust_temperature(values) -> float | None:
    """Glitch-robust raw T2AH from recent readings (~10 s).

    HAC1's rare glitches read several K too warm; the readings within
    ROBUST_BAND_K of the lowest one are averaged, so the glitch is left out
    without the low bias of a plain minimum.
    """
    values = [float(v) for v in values if _num(v) is not None]
    if not values:
        return None
    lowest = min(values)
    near = [v for v in values if v - lowest <= ROBUST_BAND_K]
    return sum(near) / len(near)


def heat_units(level: float) -> float:
    """Heat of a (continuous) level relative to level 1; 0 at level 0."""
    if level <= 0:
        return 0.0
    return (1 - math.exp(-level / MODEL_CURVATURE)) / (1 - math.exp(-1 / MODEL_CURVATURE))


def level_for_heat(heat: float) -> float:
    """Inverse of heat_units (continuous level)."""
    if heat <= 0:
        return 0.0
    inside = 1 - heat * (1 - math.exp(-1 / MODEL_CURVATURE))
    return float(LEVEL_MAX + 1) if inside <= 1e-9 else min(LEVEL_MAX + 1.0, -MODEL_CURVATURE * math.log(inside))


def hac1_setpoint(action: str, temperature: float) -> int | None:
    """Whole-degree setpoint that makes HAC1 take ``action`` at T2AH ``temperature``.

    None for "hold" when no integer holds (fraction above HOLD_FRACTION_MAX).
    """
    if action == "up":
        return math.floor(temperature + UP_OFFSET_K) + 1
    if action == "down":
        return math.floor(temperature - DOWN_OFFSET_K)
    whole = math.floor(temperature)
    return whole if temperature - whole <= HOLD_FRACTION_MAX else None


def hac1_action(setpoint: float, temperature: float) -> str:
    """What HAC1 does at its next tick with ``setpoint`` and raw T2AH ``temperature``."""
    whole = math.floor(setpoint)
    if temperature < whole:
        return "up"
    return "down" if temperature >= whole + HAC1_DOWN_BAND_K else "hold"


def hold_friendly(target: float) -> float:
    """The target moved (at most ~0.2 K) to where a whole-degree setpoint holds HAC1 safely.

    HAC1 holds for T2AH in [n, n + 0.95): air hovering right at a whole degree
    falls between two setpoints and would make HAC1 step every tick.
    """
    whole = math.floor(target)
    fraction = target - whole
    if HOLD_TARGET_BAND[0] <= fraction <= HOLD_TARGET_BAND[1]:
        return target
    if fraction < HOLD_TARGET_BAND[0]:
        below, above = whole - 1 + HOLD_TARGET_BAND[1], whole + HOLD_TARGET_BAND[0]
    else:
        below, above = whole + HOLD_TARGET_BAND[1], whole + 1 + HOLD_TARGET_BAND[0]
    return below if target - below < above - target else above


def setpoint_margin(setpoint: float, action: str, temperature: float) -> float:
    """How far (K) ``temperature`` is inside the band that makes HAC1 take ``action``."""
    whole = math.floor(setpoint)
    if action == "up":
        return whole - temperature
    if action == "down":
        return temperature - (whole + HAC1_DOWN_BAND_K)
    return min(temperature - whole, whole + HAC1_DOWN_BAND_K - temperature)


def setpoint_gives(setpoint: float, action: str, temperature: float) -> bool:
    """Whether ``setpoint`` still makes HAC1 take ``action`` with the mapping's margins."""
    if action == "up":
        return temperature + UP_OFFSET_K < setpoint
    if action == "down":
        return setpoint <= temperature - DOWN_OFFSET_K
    return setpoint == math.floor(temperature) and temperature - setpoint <= HOLD_FRACTION_MAX


class TickClock:
    """Phase of HAC1's fixed controller clock, learned from observed level changes."""

    def __init__(self, period: float = TICK_PERIOD_S) -> None:
        self.period = period
        self.anchor: float | None = None
        self.last_observation: float | None = None
        self.observations = 0

    def observe(self, seen_at: float) -> None:
        tick = seen_at - OBSERVATION_LATENCY_S
        if self.anchor is None:
            self.anchor = tick
            self.observations = 0
        else:
            k = round((tick - self.anchor) / self.period)
            predicted = self.anchor + k * self.period
            residual = tick - predicted
            if abs(residual) > PHASE_JUMP_S:
                self.anchor, self.observations = tick, 0
            else:
                weight = 0.5 if self.observations < 3 else 0.25
                self.anchor = predicted + weight * residual
        self.observations += 1
        self.last_observation = seen_at

    def known(self, now: float) -> bool:
        return (self.anchor is not None and self.last_observation is not None
                and now - self.last_observation <= PHASE_VALID_S)

    def next_tick(self, now: float) -> float | None:
        if self.anchor is None:
            return None
        k = math.floor((now - self.anchor) / self.period) + 1
        return self.anchor + k * self.period


@dataclass
class PilotInputs:
    """One look at the plant; ages in seconds, times in the caller's clock."""
    now: float
    mode: str = "native"
    afterheat_on: bool = False
    target: float | None = None
    frost_lock: bool = False
    frost_floor: float = 18.0
    level: int | None = None
    level_age: float | None = None
    level_changed_at: float | None = None
    t2ah: float | None = None
    t2ah_age: float | None = None
    t2ah_raw: float | None = None
    fan_supply: float | None = None
    water_flow: float | None = None
    outdoor: float | None = None
    extract: float | None = None
    master: bool = False
    cool_boost: bool = False
    free_cooling: bool = False
    fans_stopped: bool = False
    override: bool = False
    gain: float = PI_GAIN_DEFAULT
    integral_minutes: float = PI_INTEGRAL_MINUTES_DEFAULT


class AfterheatPilot:
    """PI + feed-forward + sigma-delta, one HAC1 step per tick, with native fallback."""

    def __init__(self, period: float = TICK_PERIOD_S) -> None:
        self.clock = TickClock(period)
        self.active = False
        self.integral = 0.0
        self.kp: float | None = None
        self.error: float | None = None
        self.level_history: deque[tuple[float, int]] = deque()
        self.raw_history: deque[tuple[float, float]] = deque()
        self.heat_lagged = 0.0
        self.heat_fast = 0.0
        self.heat_command: float | None = None
        self.control_target = 0.0
        self.kelvin_per_heat = GAIN_PRIOR
        self.fan_changed_at: float | None = None
        self.scale: float | None = None
        self.gain_num = 0.0
        self.gain_den = 0.0
        self.gain_sample: tuple[float, float, float] | None = None
        self.sigma = 0.0
        self.wanted: float | None = None
        self.last_update: float | None = None
        self.last_fan: float | None = None
        self.water_filtered: float | None = None
        self.seen_level: int | None = None
        self.setpoint: int | None = None
        self.action: str | None = None
        self.level_target: int | None = None
        self.decided_for: float | None = None
        self.command_level: int | None = None
        self.check_at: float | None = None
        self.last_step_dir = 0
        self.last_step_at: float | None = None
        self.follow_failures = 0
        self.margin = 0.0
        self.fallback_until: float | None = None
        self.fallback_reason: str | None = None
        self.note: str | None = None
        self.note_at = 0.0
        self.changes: deque[float] = deque()
        self.rate_warned_at: float | None = None
        self.result: dict[str, object] = self._snapshot(PilotInputs(now=0.0), "native", None, "HAC1 styrer selv")

    # ----------------------------------------------------------------- inputs
    @staticmethod
    def _fresh(value: object, age: object) -> bool:
        age = _num(age)
        return _num(value) is not None and age is not None and 0 <= age <= STALE_S

    def _blocker(self, inp: PilotInputs) -> str | None:
        """Why the Pi must leave HAC1 to itself right now, or None."""
        if inp.mode != "pi":
            return "HAC1 styrer selv (valgt i indstillinger)"
        if not inp.afterheat_on or inp.target is None:
            return "HAC1 styrer selv (eftervarmen er slukket)"
        if inp.override:
            return "HAC1 styrer selv (forsøg i gang)"
        if inp.fans_stopped:
            return "HAC1 styrer selv (anlægget er slukket)"
        if inp.cool_boost:
            return "HAC1 styrer selv (Køl)"
        if inp.free_cooling:
            return "HAC1 styrer selv (frikøling)"
        if not inp.master:
            return "HAC1 styrer selv (Pi er ikke master)"
        if not self._fresh(inp.level, inp.level_age) or not self._fresh(inp.t2ah, inp.t2ah_age):
            return "HAC1 styrer selv (forældet måling)"
        if self.fallback_until is not None:
            if inp.now < self.fallback_until:
                left = max(1, math.ceil((self.fallback_until - inp.now) / 60))
                return f"{self.fallback_reason} · HAC1 styrer selv {left} min endnu"
            self.fallback_until, self.fallback_reason = None, None
        return None

    def _known_reason(self, inp: PilotInputs, action: str) -> str | None:
        """Why HAC1 legitimately did not take ``action``, or None."""
        level = int(inp.level or 0)
        if action == "up" and level >= LEVEL_MAX:
            return f"Trin {LEVEL_MAX} er maksimum"
        if action == "up" and not inp.frost_lock and (
                (inp.outdoor is not None and inp.outdoor >= SUMMER_OUTDOOR_C)
                or (inp.extract is not None and inp.extract >= SUMMER_EXTRACT_C)):
            return "HAC1 sommerspærre (ude ≥ 15 °C eller T3 ≥ 24 °C)"
        if action == "down" and inp.frost_lock and level <= 1:
            return "Frostsikring: mindst trin 1"
        return None

    # --------------------------------------------------------------- control
    def _observe_level(self, inp: PilotInputs) -> None:
        if not self._fresh(inp.level, inp.level_age):
            self.seen_level = None
            return
        level = int(inp.level)
        previous = self.seen_level
        self.seen_level = level
        if previous is None or level == previous:
            return
        seen_at = _num(inp.level_changed_at)
        if seen_at is None or not 0 <= inp.now - seen_at <= 15:
            seen_at = inp.now
        self.clock.observe(seen_at)
        if not self.active or self.action is None:
            return  # nothing commanded yet (e.g. a native step right after the start)
        direction = 1 if level > previous else -1
        wanted = {"up": 1, "down": -1}.get(self.action or "hold", 0)
        if direction == wanted:
            self.follow_failures = 0
            self.last_step_dir, self.last_step_at = direction, seen_at
            self.note = None
        else:
            if self.margin >= FOLLOW_MARGIN_K:
                self.follow_failures += 1  # close to a band edge it is noise, not HAC1
            self._note(inp, "HAC1 tog et uventet trin; rettes ved næste tick")
        self.command_level, self.check_at = level, None

    def _check_follow(self, inp: PilotInputs) -> None:
        if self.check_at is None or inp.now < self.check_at:
            return
        self.check_at = None
        if self.command_level is None:
            return
        if self.action == "hold":
            if int(inp.level) == self.command_level:
                self.follow_failures = 0  # HAC1 held as asked: the misses are no longer in a row
            return
        if self.action not in ("up", "down"):
            return
        if int(inp.level) != self.command_level:
            return  # moved; judged when the change was seen
        known = self._known_reason(inp, self.action)
        if known:
            self._note(inp, known)
            return
        if self.margin < FOLLOW_MARGIN_K:
            return
        self.follow_failures += 1
        self._note(inp, f"HAC1 fulgte ikke ({self.follow_failures} gang{'e' if self.follow_failures > 1 else ''})")

    # ------------------------------------------------------- plant model
    def _delayed_level(self, at: float) -> int:
        level = self.level_history[0][1]
        for when, value in self.level_history:
            if when > at:
                break
            level = value
        return level

    def _advance_model(self, now: float, level: int, dt: float) -> None:
        """Heat units of the level as they reach T2AH (dead time + first-order lag)."""
        self.level_history.append((now, level))
        while len(self.level_history) > 2 and self.level_history[1][0] <= now - MODEL_DEAD_TIME_S - 30:
            self.level_history.popleft()
        if dt > 0:
            delayed = heat_units(self._delayed_level(now - MODEL_DEAD_TIME_S))
            self.heat_lagged += (delayed - self.heat_lagged) * (1 - math.exp(-dt / MODEL_LAG_S))

    def _lagged_at(self, now: float, ahead: float) -> float:
        """Model heat at T2AH ``ahead`` seconds from now (the levels it needs are known)."""
        value, t = self.heat_lagged, 0.0
        while t < ahead:
            step = min(2.0, ahead - t)
            t += step
            delayed = heat_units(self._delayed_level(now + t - MODEL_DEAD_TIME_S))
            value += (delayed - value) * (1 - math.exp(-step / MODEL_LAG_S))
        return value

    def _drift(self, now: float, ahead: float) -> float:
        """Expected change of the raw T2AH over the next ``ahead`` seconds.

        The measured trend of the last ~30 s (least squares) when there is one,
        else what the model expects from the levels already on their way.
        """
        points = [(t, v) for t, v in self.raw_history if now - t <= RAW_TREND_S]
        if len(points) >= 5 and points[-1][0] - points[0][0] >= RAW_TREND_S / 2:
            mean_t = sum(t for t, _v in points) / len(points)
            mean_v = sum(v for _t, v in points) / len(points)
            den = sum((t - mean_t) ** 2 for t, _v in points)
            slope = sum((t - mean_t) * (v - mean_v) for t, v in points) / den if den > 0 else 0.0
            return _clamp(slope, -0.05, 0.05) * ahead
        return self.kelvin_per_heat * (self._lagged_at(now, ahead) - self.heat_lagged)

    def _learn_gain(self, inp: PilotInputs, scale: float) -> None:
        """Kelvin per heat unit at the reference fan and lift, from how T2AH followed the model."""
        if self.fan_changed_at is not None and inp.now - self.fan_changed_at < GAIN_FAN_SETTLE_S:
            self.gain_sample = None
            return
        sample = (inp.now, float(inp.t2ah), self.heat_lagged * scale)
        previous = self.gain_sample
        if previous is None:
            self.gain_sample = sample
            return
        if inp.now - previous[0] < GAIN_SAMPLE_S:
            return
        self.gain_sample = sample
        dheat = sample[2] - previous[2]
        if abs(dheat) < 0.02:
            return
        keep = math.exp(-(inp.now - previous[0]) / GAIN_MEMORY_S)
        self.gain_num = self.gain_num * keep + (sample[1] - previous[1]) * dheat
        self.gain_den = self.gain_den * keep + dheat * dheat

    def plant_gain(self) -> float:
        """Learned K per heat unit at the reference fan and lift (prior-weighted)."""
        value = (self.gain_num + GAIN_PRIOR * GAIN_PRIOR_WEIGHT) / (self.gain_den + GAIN_PRIOR_WEIGHT)
        return _clamp(value, *GAIN_RANGE)

    def _scale(self, inp: PilotInputs, fan: float, dt: float) -> float:
        """Heat per level relative to the reference: less with more air, more with warmer water."""
        scale = FAN_REFERENCE_PERCENT / fan
        water = _num(inp.water_flow)
        if water is None or not 0 < water < 95:
            self.water_filtered = None
            return scale
        previous = self.water_filtered
        self.water_filtered = water if previous is None else previous + (water - previous) * min(1.0, dt / WATER_FILTER_S)
        lift = self.water_filtered - float(inp.target)
        if lift < WATER_MIN_LIFT_K:
            return scale
        return scale * lift / WATER_REFERENCE_LIFT_K

    def _pi(self, inp: PilotInputs, dt: float, low: int, high: int) -> float:
        """Continuous wanted level from a PI on the model-predicted steady error."""
        level = int(inp.level)
        fan = _clamp(_num(inp.fan_supply) or FAN_REFERENCE_PERCENT, 15.0, 100.0)
        scale = self._scale(inp, fan, dt)
        lowest, highest = heat_units(low), heat_units(high)
        if self.scale is not None and scale != self.scale:
            # Feed-forward: the heat needed per level follows airflow and water lift.
            heat_before = _clamp(self.integral + (self.kp or 0.0) * (self.error or 0.0), lowest, highest)
            self.integral += heat_before * (self.scale / scale - 1.0) * FEED_FORWARD
        if self.last_fan is not None and abs(fan - self.last_fan) >= FAN_STEP_PERCENT:
            self.fan_changed_at = inp.now
        if self.last_fan is None or abs(fan - self.last_fan) >= FAN_STEP_PERCENT:
            self.last_fan = fan
        self.scale = scale
        self._advance_model(inp.now, level, dt)
        # The commanded heat as it would show without the dead time (Smith
        # predictor). The quantizer gives the wanted heat on average, so the
        # continuous command is used, but never more than a level from HAC1's.
        here = heat_units(level)
        reach = max(heat_units(level + 1) - here, here - heat_units(max(level - 1, 0)))
        command = here if self.heat_command is None else _clamp(self.heat_command, here - reach, here + reach)
        if dt > 0:
            self.heat_fast += (command - self.heat_fast) * (1 - math.exp(-dt / MODEL_LAG_S))
        self._learn_gain(inp, scale)
        if _num(inp.t2ah_raw) is not None:
            self.raw_history.append((inp.now, self._raw(inp)))
        while self.raw_history and inp.now - self.raw_history[0][0] > RAW_TREND_S:
            self.raw_history.popleft()
        kelvin_per_heat = self.kelvin_per_heat = self.plant_gain() * scale
        predicted = float(inp.t2ah) + kelvin_per_heat * (self.heat_fast - self.heat_lagged)
        error = self.control_target - predicted
        kp = _clamp(float(inp.gain), *PI_GAIN_RANGE) * MODEL_LAG_S / CLOSED_LOOP_S / kelvin_per_heat
        ti = _clamp(float(inp.integral_minutes), *PI_INTEGRAL_MINUTES_RANGE) * 60.0
        if self.kp is not None and self.error is not None and kp != self.kp:
            self.integral += (self.kp - kp) * error  # bumpless gain change
        self.kp, self.error = kp, error
        candidate = self.integral + kp * error * dt / ti
        raw = candidate + kp * error
        blocked_up = error > 0 and self._known_reason(inp, "up") is not None
        # Anti-windup: also while HAC1's one step per tick (or the dwell) keeps the level behind.
        behind = level_for_heat(raw) - level
        windup = ((raw > highest and error > 0) or (raw < lowest and error < 0) or blocked_up
                  or (error > 0 and behind > WINDUP_LEVELS) or (error < 0 and behind < -WINDUP_LEVELS))
        if not windup:
            self.integral = candidate
        self.integral = _clamp(self.integral, lowest - 1.0, highest + 1.0)
        self.heat_command = _clamp(self.integral + kp * error, lowest, highest)
        return _clamp(level_for_heat(self.heat_command), low, high)

    def _plan(self, inp: PilotInputs, wanted: float, low: int, high: int) -> tuple[str, int]:
        """Next action and integer target level from the wanted level and the quantizer."""
        level = int(inp.level)
        averaged = wanted + self.sigma
        # A little hysteresis so measurement noise alone does not move the level.
        nearest = level if abs(averaged - level) < 0.5 + QUANTIZER_HYSTERESIS else math.floor(averaged + 0.5)
        target = int(_clamp(nearest, low, high))
        if level < low:
            return "up", max(target, low)
        if level > high:
            return "down", min(target, high)
        reversal_ok = (self.last_step_at is None or abs(self.error or 0.0) >= REVERSAL_URGENT_K
                       or inp.now - self.last_step_at >= REVERSAL_DWELL_TICKS * self.clock.period - DECIDE_BEFORE_S - 5)
        if target > level and (self.last_step_dir >= 0 or reversal_ok):
            return "up", target
        if target < level and (self.last_step_dir <= 0 or reversal_ok):
            return "down", target
        return "hold", target

    def _choose_setpoint(self, inp: PilotInputs, action: str, wanted: float, low: int,
                         ahead: float = 0.0) -> tuple[str, int, str | None, float]:
        """Integer setpoint for ``action``; may change the action when it cannot be had.

        Judged on HAC1's raw T2AH (glitch-robust) as the model expects it ``ahead`` seconds on.
        """
        temperature = self._raw(inp)
        if ahead > 0:
            temperature += self._drift(inp.now, ahead)
        floor_sp = max(SETPOINT_MIN, math.ceil(inp.frost_floor) if inp.frost_lock else SETPOINT_MIN)
        level = int(inp.level)
        note = None
        lean = wanted + self.sigma - level
        if lean == 0:
            lean = self.control_target - temperature
        just_above = temperature - math.floor(temperature) < HOLD_FRACTION_MIN
        if action == "hold" and (hac1_setpoint("hold", temperature) is None or (just_above and lean < 0)):
            # No whole degree holds safely: take the neighbour step the controller leans to.
            action = "up" if lean > 0 else "down"
            if action == "down" and level <= low:
                action = "up"
            note = "Intet helt setpunkt holder trinnet; tager nabotrinnet"
        if action == "down":
            sp = hac1_setpoint("down", temperature)
            if sp < floor_sp:
                held = hac1_setpoint("hold", temperature)
                action, note = ("hold", "Frostsikring: setpunktet kan ikke gå lavere") if held is not None and held >= floor_sp else ("up", note)
        if self.setpoint is not None and self.setpoint >= floor_sp and setpoint_gives(self.setpoint, action, temperature):
            return action, self.setpoint, note, temperature  # fewest setpoint changes
        sp = hac1_setpoint(action, temperature)
        if sp is None:
            sp = math.floor(temperature) + 1
            action = "up"
        return action, int(_clamp(max(sp, floor_sp), SETPOINT_MIN, SETPOINT_MAX)), note, temperature

    @staticmethod
    def _raw(inp: PilotInputs) -> float:
        """HAC1's raw T2AH (glitch-robust), never far above the filtered value."""
        filtered = float(inp.t2ah)
        raw = _num(inp.t2ah_raw)
        return filtered if raw is None or raw > filtered + RAW_GLITCH_K else raw

    def _note(self, inp: PilotInputs, text: str) -> None:
        self.note, self.note_at = text, inp.now

    def _set(self, inp: PilotInputs, setpoint: int) -> None:
        if setpoint == self.setpoint:
            return
        if self.setpoint is not None:
            self.changes.append(inp.now)
        self.setpoint = setpoint
        while self.changes and inp.now - self.changes[0] > RATE_WINDOW_S:
            self.changes.popleft()
        if len(self.changes) > RATE_WARN_CHANGES and (
                self.rate_warned_at is None or inp.now - self.rate_warned_at >= RATE_WINDOW_S):
            self.rate_warned_at = inp.now
            LOG.warning("Afterheat: %d HAC1 setpoint changes in 10 minutes", len(self.changes))

    def _decide(self, inp: PilotInputs, wanted: float, low: int, high: int, tick: float | None) -> None:
        action, target = self._plan(inp, wanted, low, high)
        ahead = 0.0 if tick is None else max(0.0, tick - inp.now)
        action, setpoint, note, temperature = self._choose_setpoint(inp, action, wanted, low, ahead)
        if setpoint != self.setpoint and self.setpoint is not None and len(self.changes) >= RATE_LIMIT_CHANGES:
            # Rate limit: keep the setpoint unless it would make HAC1 step the wrong way.
            current = hac1_action(self.setpoint, temperature)
            floor_sp = math.ceil(inp.frost_floor) if inp.frost_lock else SETPOINT_MIN
            if (int(inp.level) >= low and self.setpoint >= floor_sp
                    and (current == "hold" or current == action)):
                action, setpoint = current, self.setpoint
                note = "Begrænser ændringer af HAC1-setpunktet"
        self.margin = setpoint_margin(setpoint, action, temperature)
        changed_action = action != self.action
        self.action, self.level_target = action, target
        self._set(inp, setpoint)
        if note:
            self._note(inp, note)
        if tick is not None:
            self.decided_for = tick
            self.command_level = int(inp.level)
            self.check_at = tick + CHECK_AFTER_S
        elif changed_action or self.check_at is None:
            self.command_level = int(inp.level)
            self.check_at = inp.now + self.clock.period + DECIDE_BEFORE_S

    def _activate(self, inp: PilotInputs, low: int) -> None:
        level = int(inp.level)
        self.level_history = deque([(inp.now - MODEL_DEAD_TIME_S - 30, level)])
        self.heat_lagged = self.heat_fast = heat_units(level)  # assume T2AH has settled on the level it has
        self.heat_command = None
        self.kp, self.error = None, None
        self.integral = heat_units(max(level, low))  # bumpless: u starts at the level
        self.last_fan, self.fan_changed_at, self.water_filtered, self.scale = None, None, None, None
        self.gain_sample = None
        self.raw_history.clear()
        self.sigma, self.follow_failures, self.margin = 0.0, 0, 0.0
        self.action, self.decided_for, self.check_at = None, None, None
        self.last_step_dir, self.last_step_at = 0, None
        self.command_level, self.note = level, None
        self.active = True

    def _deactivate(self) -> None:
        self.active = False
        self.kp = None
        self.wanted = None
        self.action, self.level_target, self.decided_for, self.check_at = None, None, None, None

    def update(self, inp: PilotInputs) -> dict[str, object]:
        """Advance the controller to ``inp.now``; the result holds the setpoint to send."""
        now = inp.now
        dt = 0.0 if self.last_update is None else _clamp(now - self.last_update, 0.0, 10.0)
        self.last_update = now
        self._observe_level(inp)
        if self.active and self.follow_failures >= FOLLOW_FAIL_LIMIT:
            self.fallback_until = now + FALLBACK_S
            self.fallback_reason = f"HAC1 fulgte ikke Pi-styringen {FOLLOW_FAIL_LIMIT} gange i træk"
            LOG.warning("Afterheat: HAC1 did not follow %d ticks in a row; native for %d min",
                        FOLLOW_FAIL_LIMIT, FALLBACK_S // 60)
            self._deactivate()
        blocker = self._blocker(inp)
        if blocker is not None:
            if self.active:
                self._deactivate()
            self.setpoint = None
            state = "native" if inp.mode != "pi" else "fallback"
            self.result = self._snapshot(inp, state, None, blocker)
            return self.result
        low = 1 if inp.frost_lock else 0
        high = LEVEL_MAX
        self.control_target = hold_friendly(float(inp.target))
        if not self.active:
            self._activate(inp, low)
        self._check_follow(inp)
        if self.active and self.follow_failures >= FOLLOW_FAIL_LIMIT:
            return self.update(inp)  # falls back on the same call
        wanted = self._pi(inp, dt, low, high)
        level = int(inp.level)
        if abs(wanted - level) <= 1.0:
            self.sigma = _clamp(self.sigma + (wanted - level) * dt / self.clock.period, -1.0, 1.0)
        else:
            self.sigma *= math.exp(-dt / self.clock.period)
        self.wanted = wanted
        next_tick = self.clock.next_tick(now) if self.clock.known(now) else None
        if next_tick is None:
            self._decide(inp, wanted, low, high, None)  # state based: valid at any moment
        else:
            to_tick = next_tick - now
            since_tick = now - (next_tick - self.clock.period)
            decided = self.decided_for is not None and abs(self.decided_for - next_tick) < self.clock.period / 2
            if to_tick <= DECIDE_BEFORE_S and not decided:
                self._decide(inp, wanted, low, high, next_tick)
            elif self.setpoint is None and to_tick > FREEZE_S and since_tick > FREEZE_S:
                self._decide(inp, wanted, low, high, None)
        self.result = self._snapshot(inp, "pi", next_tick, None)
        return self.result

    # -------------------------------------------------------------- reporting
    def _reason(self, inp: PilotInputs) -> str:
        level = int(inp.level)
        temperature = float(inp.t2ah)
        target = float(inp.target)
        low = 1 if inp.frost_lock else 0
        if inp.frost_lock and level < low:
            return "Frostsikring: mindst trin 1"
        if self.note and inp.now - self.note_at <= 2 * self.clock.period and self.note.startswith(("HAC1 sommerspærre", "Trin ", "HAC1 tog", "HAC1 fulgte")):
            return self.note
        air, goal = _danish(temperature), _danish(target)
        if self.action == "up":
            if temperature < target:
                return f"Går et trin op: luft {air} °C under mål {goal} °C"
            return f"Går et trin op · luft {air} °C mod mål {goal} °C (køler af om lidt)"
        if self.action == "down":
            if temperature > target:
                return f"Går et trin ned: luft {air} °C over mål {goal} °C"
            return f"Går et trin ned · luft {air} °C mod mål {goal} °C (varmer op om lidt)"
        if inp.frost_lock and level <= low and temperature > target:
            return "Frostsikring: mindst trin 1"
        return f"Holder trin {level} · luft {air} °C mod mål {goal} °C"

    def _snapshot(self, inp: PilotInputs, state: str, next_tick: float | None, reason: str | None) -> dict[str, object]:
        level = int(inp.level) if self._fresh(inp.level, inp.level_age) else None
        target = _num(inp.target) if inp.afterheat_on else None
        t2ah = _num(inp.t2ah) if self._fresh(inp.t2ah, inp.t2ah_age) else None
        active = state == "pi"
        while self.changes and inp.now - self.changes[0] > RATE_WINDOW_S:
            self.changes.popleft()
        return {
            "afterheat_control_mode": inp.mode if inp.mode in CONTROL_MODES else "native",
            "afterheat_control_active": state,
            "afterheat_level": level,
            "afterheat_level_wanted": round(self.wanted, 1) if active and self.wanted is not None else None,
            "afterheat_level_target": self.level_target if active else None,
            "afterheat_level_max": LEVEL_MAX,
            "afterheat_next_action": self.action if active else None,
            "afterheat_next_tick_seconds": (
                max(0, int(round(next_tick - inp.now))) if active and next_tick is not None else None),
            "afterheat_target": round(target, 2) if target is not None else None,
            "afterheat_hac1_setpoint": self.setpoint if active else None,
            "afterheat_control_error": (
                round(t2ah - target, 2) if t2ah is not None and target is not None else None),
            "afterheat_control_reason": reason if reason is not None else self._reason(inp),
            "afterheat_follow_failures": self.follow_failures if active else 0,
            "afterheat_fallback_remaining_seconds": (
                max(0, int(math.ceil(self.fallback_until - inp.now)))
                if self.fallback_until is not None and self.fallback_until > inp.now else None),
            "afterheat_setpoint_changes_10min": len(self.changes),
        }


__all__ = [
    "AfterheatPilot", "PilotInputs", "TickClock", "CONTROL_MODES", "LEVEL_MAX", "TICK_PERIOD_S",
    "PI_GAIN_DEFAULT", "PI_GAIN_RANGE", "PI_INTEGRAL_MINUTES_DEFAULT", "PI_INTEGRAL_MINUTES_RANGE",
    "hold_friendly", "robust_temperature", "hac1_action", "hac1_setpoint", "setpoint_gives", "setpoint_margin",
]
