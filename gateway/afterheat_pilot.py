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
heat of every level step learned online after each settled step) takes the
coil's delay out of the loop, the gain is scheduled by the supply fan (and
the water flow temperature when a 1-Wire sensor has that role), with
feed-forward on their changes, anti-windup and a bumpless start from HAC1's
level. A sigma-delta quantizer turns the wanted level into whole levels and
HAC1 is asked for at most one step per tick.

Calm before precision (the owner's wish after the 2026-11-20 trial with the
valves opened, where one level is worth 1.5-6 K): inside a comfort band
around the target the level is left alone, a level is kept for a minimum
number of ticks unless the air is far off, and a step is only taken when the
air is expected to end up closer to the target than it is without it. Cold
that lasts is never left alone, though: when no level settles close enough,
the two neighbouring levels take turns (long-period PWM, each kept at least
the minimum time) so that the air is right on average. Under the frost lock
the target is raised to at least a degree above the frost floor HAC1 can hold
(ceil(floor) + FROST_TARGET_MARGIN_K), the band never reaches below the frost
floor, air below it is urgent, and a level that settles below it is left at
the next tick.
Everything here is pure logic: the caller passes the
measurements and the time, and sends the returned setpoint. Whenever the Pi
cannot do this safely, the result says so and the caller sends the plain
(native) setpoint instead, which is today's behaviour.
"""
from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass, replace

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
# HAC1's own reading of T2AH scatters about this much (K) around ours.
HOLD_NOISE_K = 0.03
# Air moving at least this much before the tick, heading for where the level
# settles (this far inside a range), asks for that range (see _hold_setpoint).
HOLD_MOVING_K = 0.03
HOLD_SETTLED_MARGIN_K = 0.05
# A hold setpoint is only changed for a clearly better bet (K expected harm).
HOLD_GAP_HYSTERESIS = 0.05
STEP_COST_FORBIDDEN = 100.0
# What a step HAC1 takes by itself counts as, on top of how far it moves the air (K).
UNASKED_STEP_K = 1.0
# Fractions of a degree where HAC1 holds the air safely (see _off_whole_degree).
HOLDABLE_FRACTION = (0.08, 0.88)
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

# The PI works in "heat" = kelvin at the reference fan and water lift, through
# a dead time + lag model of the coil (fitted to the 2026-11-20 logs), so it
# judges the steady state of the level HAC1 already has (Smith predictor).
MODEL_DEAD_TIME_S = 60.0
MODEL_LAG_S = 120.0
CLOSED_LOOP_S = 150.0
# Kelvin per level step at the reference fan and lift. The prior is on the
# warm side of the opened valves (1.5-2 K a level at 17-27 % fan): a coil that
# gives less (the throttled one) is then approached calmly from below. Every
# step HAC1 takes is measured once its response has (mostly) arrived and
# replaces the prior for that step; steps not seen yet follow the measured
# ones through a common factor. The valves are not linear: on 2026-11-20
# level 1 gave ~0.2 K and level 2 ~6 K (see tests/afterheat_sim.py).
PRIOR_STEP_K = 1.5
# Weight of the prior for one step against one settled measurement (~0.85).
STEP_PRIOR_WEIGHT = 0.3
FACTOR_PRIOR_WEIGHT = 0.5
FACTOR_RANGE = (0.1, 5.0)
STEP_K_RANGE = (0.05, 10.0)
# A step's response is used when this much of it has arrived (model) ...
LEARN_MIN_FRACTION = 0.3
# The robust raw T2AH (a near-minimum over ~10 s) shows a rise a few seconds late.
LEARN_SENSOR_DELAY_S = 5.0
# ... and the step is closed by itself when this much has.
LEARN_SETTLED_FRACTION = 0.85
# Old measurements fade with this time constant (the Calefa flow moves, valves get turned).
GAIN_MEMORY_S = 40 * 60.0
# A fan change during a step, or this long before it (the air is still
# settling), spoils its measurement; so does a large swing of the water lift.
LEARN_SCALE_TOLERANCE = 0.25
LEARN_FAN_SETTLE_S = 6 * 60.0
# After a level change the PI may want at most one level more until the step has had its dead time.
DEAD_TIME_GUARD_S = MODEL_DEAD_TIME_S + 15.0
# afterheat_pi_gain scales the model-based gain; the integral time is in minutes.
PI_GAIN_DEFAULT, PI_GAIN_RANGE = 1.0, (0.3, 2.0)
PI_INTEGRAL_MINUTES_DEFAULT, PI_INTEGRAL_MINUTES_RANGE = 2.0, (1.0, 20.0)
# Comfort band (K) around the target inside which the level is left alone ...
PI_BAND_DEFAULT, PI_BAND_RANGE = 0.5, (0.1, 1.5)
# ... and the fewest ticks a level is kept before a step that is not urgent.
PI_MIN_DWELL_TICKS_DEFAULT, PI_MIN_DWELL_TICKS_RANGE = 3, (1, 10)
# At the lowest level (no heat asked for) being a little cold is fine.
BAND_COLD_AT_FLOOR = 1.3
# A step is urgent (may skip the dwell) beyond this many bands, or this far too warm ...
URGENT_BANDS = 2.0
URGENT_WARM_K = 2.0
# ... or when the air is heading there within this time (measured trend).
URGENT_TREND_S = 60.0
# A step must bring the settled air at least this much closer to the target ...
IMPROVE_MARGIN_K = 0.1
# ... unless the air has been cold (beyond the band, now and where the level
# settles) this long, or this long beyond the urgent level: then more heat is
# given anyway. When the next level would overshoot further than the air is
# cold, the two levels take turns (long-period PWM, each kept at least the
# minimum dwell) so that the air is right on average instead of cold for good.
PERSIST_COLD_S = 4 * 60.0
PERSIST_URGENT_S = 60.0
# The turns follow the measured air: the error (K*s) summed since the last
# turn has to reach this before the other level is taken (larger = longer period).
PWM_SWITCH_KS = 450.0
# The sum is kept within this many switch amounts (a bounded memory).
PWM_DEBT_LIMIT = 4.0
# Under the frost lock a level is not stepped down to when it settles less than this above
# the frost floor HAC1 can hold (the whole degree at or above the frost floor).
FROST_MARGIN_K = 0.2
# Under the frost lock the Pi steers for at least this much above that whole
# degree (ceil(frost floor)), so the band and every level it keeps stay clear
# of the floor; a lower target is raised (and the reason says so).
FROST_TARGET_MARGIN_K = 1.0
# A step down is judged against that floor with its estimate times this: a
# big step (the valve's first opening, 5-6 K) is learned short and swings
# with the Calefa flow, and one tick on it costs ~2 K under the floor.
FROST_STEP_SAFETY = 1.5
# HAC1 stepping down by itself to a level that settles too close to the frost
# floor is taken back at the next tick; until the revert shows (a tick and the
# dead time, the two lags still under way) the air loses about this part of
# the step (0.51 for 60 s dead time and two 120 s lags), judged times
# FROST_STEP_SAFETY. Only when that could take the air near the floor is a
# hold setpoint that may make HAC1 step up taken to avoid it (_step_cost).
FROST_BLIP_SHARE = 0.5
# HAC1's least level under the frost lock. The valve barely opens on it (~0 K
# in the 2026-11-20 trial, the next level 5-6 K, which the model learns short):
# a step down to it is judged as losing all the heat of the level above.
FROST_MIN_LEVEL = 1
# The coil only adds heat: with the valve open at all, the air after it is never
# colder than the air before it (the 1-Wire T2). With that air this far above the
# frost floor no level can take the air below it, whatever the model says.
FROST_AIR_BEFORE_MARGIN_K = 0.3
# Under the frost lock HAC1 may go to level 0 (on/off between 0 and 1) only while
# the air into the coil and the water side (frost sensor) are both this warm:
# nothing can freeze then, and level 1 alone would overheat on mild days.
FROST_ZERO_AIR_C = 12.0
FROST_ZERO_WATER_C = 12.0
# Far too warm (URGENT_WARM_K) this long under the frost lock: one step down is allowed anyway.
WARM_ESCAPE_S = 10 * 60.0
# A level is "seen" once it has been held this long (dead time + two lags); the
# lowest air seen on it is remembered this long and then decides a frost step down
# to it, less this margin (live 2026-11-21: level 1 settled at ~20.5 C, yet the
# 'lose all heat to level 0' rule kept the Pi on level 2 all night).
LEVEL_SEEN_AFTER_S = MODEL_DEAD_TIME_S + 2 * MODEL_LAG_S
LEVEL_SEEN_MEMORY_S = 2 * 3600.0
FROST_SEEN_MARGIN_K = 0.3
# How far off the trend's guess of the air at the tick may be while taking turns (K).
PWM_GUESS_K = 0.08
# A level's settled error is judged this long after the turn to it (dead time + two lags).
PWM_JUDGE_S = MODEL_DEAD_TIME_S + 2 * MODEL_LAG_S
# HAC1 stepping off a held level this often within the window marks it as one
# it cannot hold (the air settles on a whole degree) for a while; a neighbour
# within this many bands is then kept instead.
BLIP_LIMIT = 2
BLIP_WINDOW_S = 15 * 60.0
UNHOLDABLE_S = 20 * 60
BAND_WIDEN = 1.6
# ... but only when one level is worth this many bands (no neighbour could be inside it).
WIDEN_STEP_BANDS = 2.0
# Out of the band, the air has to come this part of the band inside again to count as in it.
BAND_ENTER = 0.5
# A trend that moves the air at least this much over a dead time counts for the band.
BAND_DRIFT_K = 0.06
# In the band the integral drifts to the level held with this time constant.
BAND_BLEED_S = 600.0
# Gates that hold the level against the PI (see _plan), and those that rule a step out for a while.
HELD_GATES = ("step", "dwell", "wait", "unholdable", "pwm", "frost")
RULED_OUT_GATES = ("step", "unholdable", "pwm", "frost")


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


class LevelHeat:
    """Heat (K at the reference fan and lift) of each level, learned per level step."""

    def __init__(self) -> None:
        self.num = [0.0] * LEVEL_MAX
        self.den = [0.0] * LEVEL_MAX
        self.factor_num = 0.0
        self.factor_den = 0.0
        self.updated_at: float | None = None
        self.measured = 0
        # Every measurement so far, without the memory's fading (see lasting_step).
        self.lasting_num = [0.0] * LEVEL_MAX
        self.lasting_den = [0.0] * LEVEL_MAX
        self.cumulative = self._build()

    def factor(self) -> float:
        """Common factor on the prior for steps not measured yet."""
        value = (self.factor_num + FACTOR_PRIOR_WEIGHT) / (self.factor_den + FACTOR_PRIOR_WEIGHT)
        return _clamp(value, *FACTOR_RANGE)

    def step(self, index: int) -> float:
        """Kelvin (reference fan and lift) of the step from level ``index`` to ``index + 1``."""
        index = int(_clamp(index, 0, LEVEL_MAX - 1))
        prior = PRIOR_STEP_K * self.factor()
        value = (prior * STEP_PRIOR_WEIGHT + self.num[index]) / (STEP_PRIOR_WEIGHT + self.den[index])
        return _clamp(value, *STEP_K_RANGE)

    def lasting_step(self, index: int) -> float:
        """The step as all its measurements so far say, none faded (the prior as in ``step``).

        The model's memory fades (GAIN_MEMORY_S), so a step not taken for a
        while drifts back to the prior; under the frost lock a big step (e.g.
        the valve's first opening) must not be forgotten that way.
        """
        index = int(_clamp(index, 0, LEVEL_MAX - 1))
        prior = PRIOR_STEP_K * self.factor()
        value = (prior * STEP_PRIOR_WEIGHT + self.lasting_num[index]) / (STEP_PRIOR_WEIGHT + self.lasting_den[index])
        return _clamp(value, *STEP_K_RANGE)

    def _build(self) -> list[float]:
        cumulative = [0.0]
        for index in range(LEVEL_MAX):
            cumulative.append(cumulative[-1] + self.step(index))
        return cumulative

    def heat(self, level: float) -> float:
        """Heat of a (continuous) level; linear between and beyond the whole levels."""
        return heat_on(self.cumulative, level)

    def level(self, heat: float) -> float:
        """Continuous level for ``heat`` (inverse of ``heat``)."""
        return level_on(self.cumulative, heat)

    def learn(self, index: int, kelvin: float, weight: float, now: float) -> None:
        """One measured step (K at the reference) with its weight (0..1)."""
        if self.updated_at is not None:
            keep = math.exp(-max(0.0, now - self.updated_at) / GAIN_MEMORY_S)
            self.num = [v * keep for v in self.num]
            self.den = [v * keep for v in self.den]
            self.factor_num *= keep
            self.factor_den *= keep
        self.updated_at = now
        kelvin = _clamp(kelvin, *STEP_K_RANGE)
        self.num[index] += weight * kelvin
        self.den[index] += weight
        self.lasting_num[index] += weight * kelvin
        self.lasting_den[index] += weight
        self.factor_num += weight * kelvin / PRIOR_STEP_K
        self.factor_den += weight
        self.measured += 1
        self.cumulative = self._build()


def heat_on(cumulative: list[float], level: float) -> float:
    if level <= 0:
        return level * (cumulative[1] - cumulative[0])
    if level >= LEVEL_MAX:
        return cumulative[LEVEL_MAX] + (level - LEVEL_MAX) * (cumulative[LEVEL_MAX] - cumulative[LEVEL_MAX - 1])
    whole = int(level)
    return cumulative[whole] + (level - whole) * (cumulative[whole + 1] - cumulative[whole])


def level_on(cumulative: list[float], heat: float) -> float:
    if heat <= 0:
        return heat / (cumulative[1] - cumulative[0])
    if heat >= cumulative[LEVEL_MAX]:
        return LEVEL_MAX + (heat - cumulative[LEVEL_MAX]) / (cumulative[LEVEL_MAX] - cumulative[LEVEL_MAX - 1])
    for whole in range(LEVEL_MAX):
        if heat <= cumulative[whole + 1]:
            return whole + (heat - cumulative[whole]) / (cumulative[whole + 1] - cumulative[whole])
    return float(LEVEL_MAX)


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
    # The air before the coil (1-Wire T2) and the water-side frost sensor, when known.
    before_heater: float | None = None
    frost_sensor: float | None = None
    outdoor: float | None = None
    extract: float | None = None
    master: bool = False
    cool_boost: bool = False
    free_cooling: bool = False
    fans_stopped: bool = False
    override: bool = False
    gain: float = PI_GAIN_DEFAULT
    integral_minutes: float = PI_INTEGRAL_MINUTES_DEFAULT
    band: float = PI_BAND_DEFAULT
    min_dwell_ticks: int = PI_MIN_DWELL_TICKS_DEFAULT


class AfterheatPilot:
    """PI + feed-forward + sigma-delta in a comfort band, one HAC1 step per tick, with native fallback."""

    def __init__(self, period: float = TICK_PERIOD_S) -> None:
        self.clock = TickClock(period)
        self.active = False
        self.integral = 0.0
        self.kp: float | None = None
        self.error: float | None = None
        self.level_history: deque[tuple[float, int, float]] = deque()
        self.raw_history: deque[tuple[float, float]] = deque()
        self.heat = LevelHeat()
        self.heat_lagged = 0.0
        # The same in kelvin at the current fan: a fan change shows in it with the coil's lag.
        self.kelvin_lagged = 0.0
        self.heat_fast = 0.0
        self.heat_command: float | None = None
        self.control_target = 0.0
        self.kelvin_per_heat = 1.0
        self.fan_changed_at: float | None = None
        self.scale: float | None = None
        # Level steps being measured (see _close_episode).
        self.episodes: list[dict[str, float]] = []
        self.level_change_at: float | None = None
        self.level_change_dir = 0
        self.sigma = 0.0
        self.wanted: float | None = None
        self.last_update: float | None = None
        self.last_fan: float | None = None
        self.water_filtered: float | None = None
        self.seen_level: int | None = None
        self.setpoint: int | None = None
        self.setpoint_level: int | None = None
        self.action: str | None = None
        self.level_target: int | None = None
        self.decided_for: float | None = None
        self.command_level: int | None = None
        self.check_at: float | None = None
        self.last_step_dir = 0
        self.last_step_at: float | None = None
        self.follow_failures = 0
        self.margin = 0.0
        # Comfort band state, judged on every update (see _judge).
        self.band = (PI_BAND_DEFAULT, PI_BAND_DEFAULT)
        self.in_band = False
        self.urgent = False
        self.fan_moved = False
        self.error_now = 0.0
        self.error_settled = 0.0
        # Since when the air has been cold beyond the band (now and settled), and
        # whether it is below the frost floor while the frost lock is on.
        self.cold_since: float | None = None
        self.frost_cold = False
        # level -> (when, lowest air seen on it, fan scale then); see LEVEL_SEEN_AFTER_S.
        self.level_seen: dict[int, tuple[float, float, float]] = {}
        # Since when the air has been more than URGENT_WARM_K too warm (see WARM_ESCAPE_S).
        self.warm_since: float | None = None
        # Under the frost lock: the user's target when the Pi steers for a higher one (effective_target).
        self.target_raised_from: float | None = None
        # Under the frost lock: the level settles too close to the frost floor, up at the next tick (see _plan).
        self.frost_up = False
        # Taking turns between two levels when neither is close enough (see _pwm):
        # the lower level of the pair and the measured error summed (K*s).
        self.pwm_low: int | None = None
        self.pwm_debt = 0.0
        # Where the lower and the upper level settle (error, K), as last judged on each,
        # whether it is the upper level's turn, and since when.
        self.pwm_settled: list[float | None] = [None, None]
        self.pwm_up = False
        self.pwm_turn_at: float | None = None
        # Why the last decision held the level ("band", "dwell", "step") and the step it weighed (K).
        self.gate: str | None = None
        self.gate_step_k = 0.0
        self.gate_dir = 0
        self.band_hold = False
        self.commanded_dir = 0
        self.commanded_at: float | None = None
        # The level HAC1 left on its own (an unexpected step), to go back to.
        self.revert_level: int | None = None
        self.revert_at = 0.0
        # HAC1's own steps off a held level, and levels it cannot hold (until when).
        self.blips: deque[tuple[float, int]] = deque()
        self.unholdable: dict[int, float] = {}
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

    @staticmethod
    def _band_setting(inp: PilotInputs) -> float:
        return _clamp(_num(inp.band) or PI_BAND_DEFAULT, *PI_BAND_RANGE)

    @staticmethod
    def _dwell_ticks(inp: PilotInputs) -> int:
        return int(_clamp(round(_num(inp.min_dwell_ticks) or PI_MIN_DWELL_TICKS_DEFAULT), *PI_MIN_DWELL_TICKS_RANGE))

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
        if not self.active:
            return
        self.level_change_at, self.level_change_dir = seen_at, 1 if level > previous else -1
        if not self._fresh(inp.t2ah, inp.t2ah_age) or abs(level - previous) != 1:
            self.episodes = []
        else:
            # A step still being measured is watched until this one reaches T2AH.
            for episode in self.episodes:
                episode.setdefault("until", seen_at - OBSERVATION_LATENCY_S + MODEL_DEAD_TIME_S)
            self.episodes.append({"index": min(level, previous), "sign": 1.0 if level > previous else -1.0,
                                  "t0": seen_at - OBSERVATION_LATENCY_S, "temperature": self._raw(inp),
                                  "kelvin": self.kelvin_lagged, "scale": self.scale or 1.0})
        if self.action is None:
            return  # nothing commanded yet (e.g. a native step right after the start)
        direction = 1 if level > previous else -1
        wanted = {"up": 1, "down": -1}.get(self.action or "hold", 0)
        if wanted != direction and self.commanded_dir == direction and self.commanded_at is not None and (
                seen_at - self.commanded_at <= self.clock.period + DECIDE_BEFORE_S + 5):
            wanted = direction  # asked for within the last tick (the decision moved on to a hold since)
        if direction == wanted:
            self.commanded_dir = 0  # that command is used up
            self.follow_failures = 0
            self.last_step_dir, self.last_step_at = direction, seen_at
            self.note = None
        elif direction > 0 and inp.frost_lock and self._fresh(inp.t2ah, inp.t2ah_age) and (
                self._raw(inp) < math.ceil(float(inp.frost_floor))):
            # The frost floor makes HAC1 step up by itself (no setpoint may go
            # below it): not a miss, not a blip, and not taken back.
            self.commanded_dir = 0
            self.last_step_dir, self.last_step_at = direction, seen_at
            self.revert_level = None
            self._note(inp, "Frostsikring: HAC1 gik selv et trin op")
        else:
            if self.margin >= FOLLOW_MARGIN_K and not self.band_hold:
                self.follow_failures += 1  # close to a band edge (or in the comfort band) it is noise, not HAC1
            self._note(inp, "HAC1 tog et uventet trin; rettes ved næste tick")
            self.revert_level, self.revert_at = previous, seen_at
            if self.action == "hold":
                # HAC1 stepped off a level it was asked to hold: noted, and a level
                # it keeps stepping off is avoided for a while (see _plan).
                self.blips.append((seen_at, previous))
                while self.blips and seen_at - self.blips[0][0] > BLIP_WINDOW_S:
                    self.blips.popleft()
                if sum(1 for _at, blipped in self.blips if blipped == previous) >= BLIP_LIMIT:
                    self.unholdable[previous] = seen_at + UNHOLDABLE_S
                    LOG.info("Afterheat: level %d is not held by HAC1 (air on a whole degree); avoided for %d min",
                             previous, UNHOLDABLE_S // 60)
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
    def _delayed(self, at: float) -> tuple[int, float]:
        """Level and fan scale as they were at ``at``."""
        _when, level, scale = self.level_history[0]
        for when, value, factor in self.level_history:
            if when > at:
                break
            level, scale = value, factor
        return level, scale

    def _delayed_level(self, at: float) -> int:
        return self._delayed(at)[0]

    def _advance_model(self, now: float, level: int, dt: float, scale: float) -> None:
        """Heat of the level as it reaches T2AH (dead time + first-order lag)."""
        self.level_history.append((now, level, scale))
        while len(self.level_history) > 2 and self.level_history[1][0] <= now - MODEL_DEAD_TIME_S - 30:
            self.level_history.popleft()
        if dt > 0:
            delayed_level, delayed_scale = self._delayed(now - MODEL_DEAD_TIME_S)
            delayed = self.heat.heat(delayed_level)
            keep = 1 - math.exp(-dt / MODEL_LAG_S)
            self.heat_lagged += (delayed - self.heat_lagged) * keep
            self.kelvin_lagged += (delayed_scale * delayed - self.kelvin_lagged) * keep

    def _lagged_at(self, now: float, ahead: float) -> float:
        """Model heat at T2AH ``ahead`` seconds from now (the levels it needs are known)."""
        value, t = self.heat_lagged, 0.0
        while t < ahead:
            step = min(2.0, ahead - t)
            t += step
            delayed = self.heat.heat(self._delayed_level(now + t - MODEL_DEAD_TIME_S))
            value += (delayed - value) * (1 - math.exp(-step / MODEL_LAG_S))
        return value

    def _trend(self, now: float) -> float | None:
        """Measured slope (K/s) of the raw T2AH over the last ~30 s, or None."""
        points = [(t, v) for t, v in self.raw_history if now - t <= RAW_TREND_S]
        if len(points) < 5 or points[-1][0] - points[0][0] < RAW_TREND_S / 2:
            return None
        mean_t = sum(t for t, _v in points) / len(points)
        mean_v = sum(v for _t, v in points) / len(points)
        den = sum((t - mean_t) ** 2 for t, _v in points)
        slope = sum((t - mean_t) * (v - mean_v) for t, v in points) / den if den > 0 else 0.0
        return _clamp(slope, -0.05, 0.05)

    def _drift(self, now: float, ahead: float) -> float:
        """Expected change of the raw T2AH over the next ``ahead`` seconds.

        The measured trend of the last ~30 s (least squares) when there is one,
        else what the model expects from the levels already on their way.
        """
        slope = self._trend(now)
        if slope is not None:
            return slope * ahead
        return self.kelvin_per_heat * (self._lagged_at(now, ahead) - self.heat_lagged)

    def _close_episode(self, inp: PilotInputs, episode: dict[str, float]) -> None:
        """Learn a level step from how far T2AH followed it (once enough of it has arrived).

        The model's error since the step is put down to the step's heat, in
        proportion to how much of the step has reached T2AH (dead time + lag).
        """
        self.episodes.remove(episode)
        if self.scale is None:
            return
        scale = self.scale
        if abs(scale / episode["scale"] - 1.0) > LEARN_SCALE_TOLERANCE or (
                self.fan_changed_at is not None and self.fan_changed_at >= episode["t0"] - LEARN_FAN_SETTLE_S):
            return  # the airflow moved: the step cannot be told from it
        elapsed = inp.now - episode["t0"] - MODEL_DEAD_TIME_S - LEARN_SENSOR_DELAY_S
        fraction = 0.0 if elapsed <= 0 else 1 - math.exp(-elapsed / MODEL_LAG_S)
        if fraction < LEARN_MIN_FRACTION:
            return
        index = int(episode["index"])
        modelled = self.kelvin_lagged - episode["kelvin"]  # follows the water lift too
        residual = (self._raw(inp) - episode["temperature"]) - modelled
        measured = self.heat.step(index) + episode["sign"] * residual / (scale * fraction)
        before = list(self.heat.cumulative)
        self.heat.learn(index, measured, fraction, inp.now)
        # The model state keeps its meaning in levels when the heat table changes.
        def moved(value: float) -> float:
            return self.heat.heat(level_on(before, value))
        for other in self.episodes:
            other["kelvin"] = self.scale * moved(other["kelvin"] / self.scale)
        self.heat_lagged = moved(self.heat_lagged)
        self.kelvin_lagged = self.scale * moved(self.kelvin_lagged / self.scale)
        self.heat_fast = moved(self.heat_fast)
        self.integral = moved(self.integral)
        if self.heat_command is not None:
            self.heat_command = moved(self.heat_command)
        LOG.info("Afterheat: step %d->%d measured %.2f K (ref.), now %.2f K", index, index + 1,
                 measured, self.heat.step(index))

    def _track_episodes(self, inp: PilotInputs) -> None:
        for episode in list(self.episodes):
            elapsed = inp.now - episode["t0"] - MODEL_DEAD_TIME_S
            settled = elapsed > 0 and 1 - math.exp(-elapsed / MODEL_LAG_S) >= LEARN_SETTLED_FRACTION
            if settled or inp.now >= episode.get("until", math.inf):
                self._close_episode(inp, episode)

    def plant_gain(self) -> float:
        """Common factor of the learned heat per level against the prior."""
        return self.heat.factor()

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

    def _cold_band(self, inp: PilotInputs, level: int, low: int) -> float:
        """How far (K) below the target the air may be on ``level`` and count as in the band."""
        cold = self._band_setting(inp) * (BAND_COLD_AT_FLOOR if level <= low else 1.0)
        if inp.frost_lock:
            # The cold edge of the band never goes below the frost floor.
            cold = min(cold, max(0.0, float(inp.target) - float(inp.frost_floor)))
        return cold

    def _judge(self, inp: PilotInputs, scale: float, level: int, low: int, high: int) -> None:
        """Comfort band: is the air (now and where the level settles) close enough to leave it be?"""
        band = self._band_setting(inp)
        target = float(inp.target)
        cold = self._cold_band(inp, level, low)
        warm = band
        self.band = (cold, warm)
        # The glitch-robust raw T2AH: the 15 s minimum (filtered) reads a little low.
        air = self._raw(inp)
        error_now = air - target
        if (self.level_change_at is not None and inp.now - self.level_change_at >= LEVEL_SEEN_AFTER_S
                and not self.fan_moved):
            seen = self.level_seen.get(level)
            fresh = seen is not None and seen[0] >= self.level_change_at
            self.level_seen[level] = (inp.now, min(air, seen[1]) if fresh else air, scale)
        # Under the frost lock, air below the frost floor is always urgent (and HAC1 steps up by itself).
        self.frost_cold = bool(inp.frost_lock and air < float(inp.frost_floor))
        error_settled = error_now + scale * self.heat.heat(level) - self.kelvin_lagged
        self.error_now, self.error_settled = error_now, error_settled
        # Hysteresis: once out, the air has to come well inside again (a step
        # that cannot bring it there is weighed by _plan instead).
        enter = 1.0 if self.in_band else BAND_ENTER
        slope = self._trend(inp.now)
        # A clear drift (the Calefa flow, the air before the coil) is met a dead time early.
        drift = slope * MODEL_DEAD_TIME_S if slope is not None and abs(slope) * MODEL_DEAD_TIME_S >= BAND_DRIFT_K else 0.0
        self.in_band = (low <= level <= high and -cold * enter <= error_now <= warm * enter
                        and -cold * enter <= error_settled <= warm * enter
                        and -cold <= error_now + drift <= warm)
        heading = error_now + (max(0.0, slope) * URGENT_TREND_S if slope is not None else 0.0)
        # Right after a fan change the model knows where the air is going before it shows.
        fan_moved = self.fan_moved = (self.fan_changed_at is not None
                                      and inp.now - self.fan_changed_at < MODEL_DEAD_TIME_S + 2 * MODEL_LAG_S)
        self.urgent = (level < low or level > high or error_now > URGENT_WARM_K or self.frost_cold
                       or error_now < -URGENT_BANDS * cold or max(error_now, heading) > URGENT_BANDS * warm
                       or (fan_moved and not -URGENT_BANDS * cold <= error_settled <= URGENT_BANDS * warm))
        if error_now < -cold and error_settled < -cold:
            if self.cold_since is None:
                self.cold_since = inp.now
        else:
            self.cold_since = None
        if error_now > URGENT_WARM_K:
            if self.warm_since is None:
                self.warm_since = inp.now
        else:
            self.warm_since = None

    def _persistent_cold(self, inp: PilotInputs) -> bool:
        """The air has been cold long enough (and will stay so on this level) to give more heat anyway."""
        if self.frost_cold:
            return True
        if self.cold_since is None:
            return False
        cold = self.band[0]
        lasted = inp.now - self.cold_since
        beyond_urgent = self.error_now < -URGENT_BANDS * cold and self.error_settled < -URGENT_BANDS * cold
        return lasted >= PERSIST_COLD_S or (beyond_urgent and lasted >= PERSIST_URGENT_S)

    def _pi(self, inp: PilotInputs, dt: float, low: int, high: int) -> float:
        """Continuous wanted level from a PI on the model-predicted steady error."""
        level = int(inp.level)
        fan = _clamp(_num(inp.fan_supply) or FAN_REFERENCE_PERCENT, 15.0, 100.0)
        scale = self._scale(inp, fan, dt)
        heat = self.heat.heat
        lowest, highest = heat(low), heat(high)
        if self.scale is not None and scale != self.scale:
            # Feed-forward: the heat needed per level follows airflow and water lift.
            heat_before = _clamp(self.integral + (self.kp or 0.0) * (self.error or 0.0), lowest, highest)
            self.integral += heat_before * (self.scale / scale - 1.0) * FEED_FORWARD
        if self.last_fan is not None and abs(fan - self.last_fan) >= FAN_STEP_PERCENT:
            self.fan_changed_at = inp.now
        if self.last_fan is None or abs(fan - self.last_fan) >= FAN_STEP_PERCENT:
            self.last_fan = fan
        self.scale = scale
        self._advance_model(inp.now, level, dt, scale)
        # The commanded heat as it would show without the dead time (Smith
        # predictor). The quantizer gives the wanted heat on average, so the
        # continuous command is used, but never more than a level from HAC1's.
        here = heat(level)
        above, below = heat(level + 1), heat(max(level - 1, 0))
        command = here if self.heat_command is None else _clamp(self.heat_command, below, above)
        if dt > 0:
            self.heat_fast += (command - self.heat_fast) * (1 - math.exp(-dt / MODEL_LAG_S))
        self._track_episodes(inp)
        if _num(inp.t2ah_raw) is not None:
            self.raw_history.append((inp.now, self._raw(inp)))
        while self.raw_history and inp.now - self.raw_history[0][0] > RAW_TREND_S:
            self.raw_history.popleft()
        self.kelvin_per_heat = scale
        predicted = float(inp.t2ah) + scale * self.heat_fast - self.kelvin_lagged
        error = self.control_target - predicted
        kp = _clamp(float(inp.gain), *PI_GAIN_RANGE) * MODEL_LAG_S / CLOSED_LOOP_S / scale
        ti = _clamp(float(inp.integral_minutes), *PI_INTEGRAL_MINUTES_RANGE) * 60.0
        if self.kp is not None and self.error is not None and kp != self.kp:
            self.integral += (self.kp - kp) * error  # bumpless gain change
        self.kp, self.error = kp, error
        self._judge(inp, scale, level, low, high)
        # Until a new level has had its dead time, the PI may not ask for more than one level beyond it.
        guarded = self.level_change_at is not None and inp.now - self.level_change_at < DEAD_TIME_GUARD_S
        # A level held by the gates (see _plan) is a blocked actuator for the PI:
        # the integral does not wind up against it, and while a step is ruled
        # out for good (it would overshoot, HAC1 cannot hold the next level, or
        # the levels take turns) the integral drifts to the level held, so no
        # stale demand is left when the gate opens.
        held = self.action == "hold" and self.gate in HELD_GATES
        if self.in_band or self.pwm_low is not None or (held and self.gate in RULED_OUT_GATES):
            # Calm: the level is held; the integral drifts to it so leaving the band is bumpless.
            self.integral += (here - self.integral) * (1 - math.exp(-dt / BAND_BLEED_S)) if dt > 0 else 0.0
        else:
            candidate = self.integral + kp * error * dt / ti
            raw = candidate + kp * error
            blocked_up = error > 0 and self._known_reason(inp, "up") is not None
            # Anti-windup: also while HAC1's one step per tick (or the dwell) keeps the level behind.
            behind = self.heat.level(raw) - level
            windup = ((raw > highest and error > 0) or (raw < lowest and error < 0) or blocked_up
                      or (held and error * self.gate_dir > 0)
                      or (error > 0 and (behind > WINDUP_LEVELS or guarded))
                      or (error < 0 and (behind < -WINDUP_LEVELS or guarded)))
            if not windup:
                self.integral = candidate
        self.integral = _clamp(self.integral, lowest - 1.0, highest + 1.0)
        if guarded:
            self.integral = _clamp(self.integral, below, above)
        self.heat_command = _clamp(self.integral + kp * error, lowest, highest)
        if guarded:
            self.heat_command = _clamp(self.heat_command, below, above)
        return _clamp(self.heat.level(self.heat_command), low, high)

    def _unholdable(self, inp: PilotInputs, level: int) -> bool:
        """HAC1 kept stepping off ``level`` lately (the air settles on a whole degree there)."""
        return level in self.unholdable

    def _acceptable(self, inp: PilotInputs, error: float) -> bool:
        """Close enough to stay on instead of a level HAC1 cannot hold.

        The band, a little wider when one level is worth much more than the
        band (then no neighbour could be inside it). Under the frost lock never
        a level that settles too close to the frost floor (_below_frost_floor):
        HAC1 would only step back up from it.
        """
        if self._below_frost_floor(inp, error):
            return False
        cold, warm = self.band
        level = int(inp.level)
        step = (self.scale or 1.0) * min(self.heat.step(level), self.heat.step(max(level - 1, 0)))
        widen = BAND_WIDEN if step > WIDEN_STEP_BANDS * self._band_setting(inp) else 1.0
        return -cold * widen <= error <= warm * widen

    def _away_from_unholdable(self, inp: PilotInputs, level: int, low: int, high: int) -> int:
        """A neighbour that settles close enough (wider band) and that HAC1 can hold, or 0."""
        best, best_error = 0, math.inf
        for move in (1, -1):
            if not low <= level + move <= high or self._unholdable(inp, level + move):
                continue
            if move < 0 and self._down_below_frost_floor(inp, level):
                continue
            error = self._neighbour_error(level, move)
            if self._acceptable(inp, error) and abs(error) < best_error:
                best, best_error = move, abs(error)
        return best

    def _off_whole_degree(self, inp: PilotInputs, level: int, low: int, high: int) -> int:
        """A neighbour level to settle on when this one leaves the air on a whole degree.

        HAC1 cannot hold the air within a few hundredths of a whole degree (no
        setpoint keeps it from stepping), so a level that settles there would
        make it step now and then. When a neighbour also settles well inside
        the band, away from a whole degree, and the dwell is over, it is
        taken once (-1/+1), else 0.
        """
        if self._dwell_left(inp) > 0 or self.fan_moved:
            return 0
        target = float(inp.target)
        settled = target + self.error_settled

        def holdable(air: float) -> bool:
            fraction = air - math.floor(air)
            return HOLDABLE_FRACTION[0] <= fraction <= HOLDABLE_FRACTION[1]
        if holdable(settled):
            return 0
        cold, warm = self.band
        best, best_error = 0, math.inf
        for move in (1, -1):
            if not (max(low, 0) <= level + move <= high):
                continue
            error = self._neighbour_error(level, move)
            air = target + error
            if move < 0 and self._down_below_frost_floor(inp, level):
                continue
            if -cold * BAND_ENTER <= error <= warm * BAND_ENTER and holdable(air) and abs(error) < best_error:
                best, best_error = move, abs(error)
        return best

    def _dwell_left(self, inp: PilotInputs) -> float:
        """Seconds until a step that is not urgent may follow the last one."""
        if self.last_step_at is None:
            return 0.0
        needed = self._dwell_ticks(inp) * self.clock.period - DECIDE_BEFORE_S - 5
        return max(0.0, needed - (inp.now - self.last_step_at))

    def _overshoots(self, inp: PilotInputs, level: int, direction: int) -> bool:
        """A step from ``level`` would not leave the settled air closer to the target (see _plan)."""
        step_k = (self.scale or 1.0) * self.heat.step(level if direction > 0 else level - 1)
        after = self.error_settled + direction * step_k
        margin = IMPROVE_MARGIN_K if after * self.error_settled < 0 else 0.0
        return abs(after) >= abs(self.error_settled) - margin

    @staticmethod
    def effective_target(inp: PilotInputs) -> float:
        """The target the Pi steers for: under the frost lock at least ceil(frost floor) + FROST_TARGET_MARGIN_K."""
        target = float(inp.target)
        if not inp.frost_lock:
            return target
        return max(target, math.ceil(float(inp.frost_floor)) + FROST_TARGET_MARGIN_K)

    @staticmethod
    def _below_frost_floor(inp: PilotInputs, error: float) -> bool:
        """Under the frost lock: air settling at ``error`` from the target is too close to the frost floor.

        The one gate for every step down (or neighbour) the Pi may choose or
        price: _plan, the revert of HAC1's own step, _off_whole_degree,
        _away_from_unholdable, _pwm (also HAC1's own step below the pair) and
        _step_cost; and the level held: one settling here is left upwards at
        the next tick (_plan), whatever the gates say. Judged against the floor
        HAC1 can hold, ceil(frost floor) + FROST_MARGIN_K: no setpoint below the
        whole degree above the frost floor may be sent, so HAC1 steps up by
        itself below it (e.g. 17.5 C: below 18 C) and a level settling there
        would only come back up (a down/up cycle). The comfort band's cold edge
        (_cold_band) and the urgent frost_cold flag stay on the raw configured
        floor: they say where the air is too cold, this says which levels are
        worth stepping down to.
        """
        if AfterheatPilot.coil_warm(inp):
            return False  # nothing can freeze: the floor only matters for a cold coil
        settle = float(inp.target) + error
        before = _num(inp.before_heater)
        if before is not None and -30 < before < 60:
            settle = max(settle, before)  # the coil never cools the air
        return inp.frost_lock and settle < math.ceil(float(inp.frost_floor)) + FROST_MARGIN_K

    @staticmethod
    def coil_warm(inp: PilotInputs) -> bool:
        """The air into the coil and its water side are both warm enough that nothing can freeze."""
        air, water = _num(inp.before_heater), _num(inp.frost_sensor)
        return air is not None and water is not None and air >= FROST_ZERO_AIR_C and water >= FROST_ZERO_WATER_C

    @staticmethod
    def frost_min_level(inp: PilotInputs) -> int:
        """Lowest level the Pi may use: 1 under the frost lock, 0 when the coil is warm on both sides."""
        if not inp.frost_lock or AfterheatPilot.coil_warm(inp):
            return 0
        return FROST_MIN_LEVEL

    def _neighbour_error(self, level: int, move: int, error: float | None = None) -> float:
        """Where the air settles (error, K) one level ``move`` (+1/-1) from ``level``."""
        error = self.error_settled if error is None else error
        return error + move * (self.scale or 1.0) * self.heat.step(level if move > 0 else level - 1)

    def _down_below_frost_floor(self, inp: PilotInputs, level: int, error: float | None = None) -> bool:
        """Under the frost lock: one level down from ``level`` settles too close to the frost floor.

        Judged with the larger of the step's estimate now and its lasting one
        (LevelHeat.lasting_step), so a big step the model has not seen for a
        while is not taken for a small one, times FROST_STEP_SAFETY; a step down
        to the frost minimum level (FROST_MIN_LEVEL) as losing all the heat
        above level 0. One tick on a level far below the floor costs ~2 K of
        air under it (dead time), so the doubt goes to the warm side.
        """
        if not inp.frost_lock or level <= 0 or self.coil_warm(inp):
            return False
        before = _num(inp.before_heater)
        if before is not None and before >= math.ceil(float(inp.frost_floor)) + FROST_MARGIN_K + FROST_AIR_BEFORE_MARGIN_K:
            return False  # the air into the coil is already above the floor HAC1 can hold
        error = self.error_settled if error is None else error
        if (self.warm_since is not None and inp.now - self.warm_since >= WARM_ESCAPE_S
                and float(inp.target) + error - (self.scale or 1.0) * max(
                    self.heat.step(level - 1), self.heat.lasting_step(level - 1)) >= float(inp.frost_floor) + 0.5):
            # Far too warm for long (live 2026-11-20: level 2 held at +5 K for
            # 80 min): one step down is allowed. HAC1 never gets a setpoint below
            # ceil(floor), so it steps up by itself should the air reach it, and
            # the frost revert takes the level back up at the next tick.
            return False
        seen = self.level_seen.get(level - 1)
        if (seen is not None and inp.now - seen[0] <= LEVEL_SEEN_MEMORY_S
                and (self.scale or 1.0) >= 0.9 * seen[2]):
            # Seen lately at no more air than now: the air that level really
            # settled at decides (with more air now, the model rule below does).
            return seen[1] - FROST_SEEN_MARGIN_K < math.ceil(float(inp.frost_floor)) + FROST_MARGIN_K
        first = 0 if level - 1 <= FROST_MIN_LEVEL else level - 1
        step = sum(max(self.heat.step(index), self.heat.lasting_step(index)) for index in range(first, level))
        return self._below_frost_floor(inp, error - (self.scale or 1.0) * step * FROST_STEP_SAFETY)

    def _frost_blip_unsafe(self, inp: PilotInputs, level: int) -> bool:
        """Under the frost lock: HAC1 stepping down from ``level`` for one tick could take the air near the floor.

        HAC1's own step down to a level that settles too close to the frost
        floor is taken back at the next tick (_plan), so the air loses only
        FROST_BLIP_SHARE of the step's heat (the larger of its estimates, times
        FROST_STEP_SAFETY, as in _down_below_frost_floor). Only when that could
        take the air now below the floor HAC1 can hold is such a step worth
        forcing a step up to avoid (_step_cost). A step down to the frost
        minimum level (FROST_MIN_LEVEL) always is: the valve's first opening is
        learned short and swings with the Calefa flow (a tick on it cost 2.5 K).
        """
        if not inp.frost_lock or level <= 0:
            return False
        if level - 1 <= FROST_MIN_LEVEL:
            return True
        step = max(self.heat.step(level - 1), self.heat.lasting_step(level - 1))
        floor = math.ceil(float(inp.frost_floor)) + FROST_MARGIN_K
        return self._raw(inp) - FROST_BLIP_SHARE * FROST_STEP_SAFETY * (self.scale or 1.0) * step < floor

    def _fresh_frost_trap(self, inp: PilotInputs, level: int) -> bool:
        """Just stepped down (by the Pi or HAC1) to a level the Pi would not have stepped down to.

        Until the step has reached the air (dead time and two lags) the model's
        settled air on the new level is only as good as its step, so the level
        is judged as a step down into it would be (_down_below_frost_floor).
        """
        return (self.level_change_dir < 0 and self.level_change_at is not None
                and inp.now - self.level_change_at < PWM_JUDGE_S
                and self._down_below_frost_floor(inp, level + 1, self._neighbour_error(level, 1)))

    def _end_pwm(self) -> None:
        if self.pwm_low is not None:
            LOG.info("Afterheat: taking turns on levels %d and %d ends", self.pwm_low, self.pwm_low + 1)
        self.pwm_low, self.pwm_debt, self.pwm_settled = None, 0.0, [None, None]
        self.pwm_up, self.pwm_turn_at = False, None

    def _pwm(self, inp: PilotInputs, level: int, low: int, high: int) -> tuple[str, int] | None:
        """Long-period PWM between two neighbouring levels when neither leaves the air close enough.

        The lower level is too cold and the upper one too warm by more than the
        cold is worth, so the levels take turns: the measured error is summed
        (``pwm_debt``, K*s) and the other level is taken when the sum has come
        PWM_SWITCH_KS the other way and the minimum dwell is over, so the air
        is right on average. Air below the frost floor goes up at once. Ends
        (None) when a level would do on its own, the level leaves the pair or
        the fan moves.
        """
        lower = self.pwm_low
        if lower is None:
            return None
        # HAC1 stepping one further out by itself, on the side whose turn it
        # is, is left alone: the summed error (measured) makes up for it, and
        # taking it back would cost two more level changes.
        # Never one that settles too close to the frost floor (taken back up, see _plan).
        cold_side = [lower] + ([lower - 1] if lower - 1 >= low and not (
            level == lower - 1 and self._below_frost_floor(inp, self.error_settled)) else [])
        warm_side = [lower + 1] + ([lower + 2] if lower + 2 <= high else [])
        members = cold_side + warm_side
        if level not in members and self.revert_level in members and abs(
                level - self.revert_level) == 1 and not self.fan_moved:
            return None  # HAC1 stepped out of the pair by itself: taken back first (see _plan)
        if level not in members or not low <= lower < high or self.fan_moved:
            self._end_pwm()
            return None
        # Where each level settles is judged on that level once the last turn
        # has (mostly) reached the air: the model's step may still be learning.
        if level in (lower, lower + 1) and (
                self.level_change_at is None or inp.now - self.level_change_at >= PWM_JUDGE_S):
            self.pwm_settled[level - lower] = self.error_settled
        cold_low, warm_high = self.pwm_settled
        if ((cold_low is not None and cold_low >= -self._cold_band(inp, lower, low))
                or (cold_low is not None and self._below_frost_floor(inp, cold_low))
                or (warm_high is not None and warm_high <= self.band[1])
                or (cold_low is not None and warm_high is not None and abs(warm_high) < abs(cold_low) - (
                    IMPROVE_MARGIN_K if warm_high * cold_low < 0 else 0.0))):
            self._end_pwm()  # one of the two is close enough, or the upper one is the better bet
            return None
        self.revert_level = None  # a step HAC1 takes by itself is undone by the turn below
        needed = self._dwell_ticks(inp) * self.clock.period - DECIDE_BEFORE_S - 5
        turn_left = max(self._dwell_left(inp), 0.0 if self.pwm_turn_at is None
                        else needed - (inp.now - self.pwm_turn_at))
        if not self.pwm_up and (self.frost_cold or (self.pwm_debt <= -PWM_SWITCH_KS and turn_left <= 0)):
            self.pwm_up, self.pwm_turn_at = True, inp.now
        elif self.pwm_up and self.pwm_debt >= PWM_SWITCH_KS and turn_left <= 0 and not self.frost_cold:
            self.pwm_up, self.pwm_turn_at = False, inp.now
        wanted = lower + (1 if self.pwm_up else 0)
        if level in (warm_side if self.pwm_up else cold_side):
            self.gate, self.gate_dir = "pwm", -1 if self.pwm_up else 1
            return "hold", level
        self.gate_dir = 1 if wanted > level else -1
        return ("up" if wanted > level else "down"), wanted

    def _plan(self, inp: PilotInputs, wanted: float, low: int, high: int) -> tuple[str, int]:
        """Next action and integer target level from the wanted level, the band and the quantizer."""
        level = int(inp.level)
        self.gate, self.gate_dir, self.gate_step_k = None, 0, 0.0
        self.frost_up = False
        averaged = wanted + self.sigma
        # A little hysteresis so measurement noise alone does not move the level.
        nearest = level if abs(averaged - level) < 0.5 + QUANTIZER_HYSTERESIS else math.floor(averaged + 0.5)
        target = int(_clamp(nearest, low, high))
        if level < low:
            return "up", max(target, low)
        if level > high:
            return "down", min(target, high)
        pwm = self._pwm(inp, level, low, high)
        if pwm is not None:
            return pwm
        for blipped in [k for k, until in self.unholdable.items() if inp.now >= until]:
            del self.unholdable[blipped]
        # Under the frost lock a level that settles too close to the frost floor
        # (or air below the floor) is never kept: up at the next tick, whatever
        # the improve margin, the band or the gates say (HAC1's own step down
        # into it is taken back the same way).
        frost_up = self.frost_up = inp.frost_lock and level < high and (
            self.frost_cold or self._below_frost_floor(inp, self.error_settled) or self._fresh_frost_trap(inp, level))
        if self.revert_level is not None:
            back = self.revert_level - level
            if back == 0 or abs(back) > 1 or inp.now - self.revert_at > 3 * self.clock.period:
                self.revert_level = None
            elif back > 0 and frost_up:
                self.gate_dir = back
                return "up", self.revert_level
            elif self._unholdable(inp, self.revert_level) and self._acceptable(inp, self.error_settled):
                self.revert_level = None  # HAC1 cannot hold that one: stay on this one
            else:
                # Undo HAC1's own step unless it left the air clearly better off
                # (never back down to a level that settles too close to the frost floor).
                after = self._neighbour_error(level, back)
                if (abs(after) <= abs(self.error_settled) + IMPROVE_MARGIN_K and low <= self.revert_level <= high
                        and not (back < 0 and self._down_below_frost_floor(inp, level))):
                    self.gate_dir = back
                    return ("up" if back > 0 else "down"), self.revert_level
                self.revert_level = None
        if frost_up:
            self.gate_dir = 1
            return "up", level + 1
        if self._unholdable(inp, level):
            move = self._away_from_unholdable(inp, level, low, high)
            if move:
                self.gate_dir = move
                return ("up" if move > 0 else "down"), level + move
        if self.in_band:
            move = self._off_whole_degree(inp, level, low, high)
            if move:
                self.gate_dir = move
                return ("up" if move > 0 else "down"), level + move
            self.gate = "band"
            return "hold", level
        persistent_cold = self._persistent_cold(inp) and level < high
        upper = self.error_settled + (self.scale or 1.0) * self.heat.step(level)
        below_floor = self._below_frost_floor(inp, self.error_settled)
        if persistent_cold and upper > self.band[1] and not below_floor and self._overshoots(inp, level, 1):
            # Cold for a while, and the next level would overshoot (out of the band) further: take turns.
            self._end_pwm()
            self.pwm_low, self.pwm_debt, self.pwm_settled = level, -PWM_SWITCH_KS, [self.error_settled, None]
            LOG.info("Afterheat: no level settles near the target; taking turns on levels %d and %d",
                     level, level + 1)
            pwm = self._pwm(inp, level, low, high)
            if pwm is not None:
                return pwm
        if persistent_cold and target <= level:
            target = level + 1  # cold for a while: more heat, whatever the integral says
        if target == level:
            if not self.urgent or self.error_settled == 0:
                return "hold", target
            # Far off: step toward the target now rather than wait for the integral.
            target = level + (1 if self.error_settled < 0 else -1)
            if not low <= target <= high:
                return "hold", level
        direction = 1 if target > level else -1
        # Only a step that leaves the air closer to the target than it settles
        # without it (by a margin when it crosses the target: no 1-level dithering).
        step_k = (self.scale or 1.0) * self.heat.step(level if direction > 0 else level - 1)
        after = self.error_settled + direction * step_k
        margin = IMPROVE_MARGIN_K if after * self.error_settled < 0 else 0.0
        if direction < 0 and self._down_below_frost_floor(inp, level):
            # Under the frost lock a level that settles below the frost floor is not taken, however warm it is now.
            self.gate, self.gate_dir, self.gate_step_k = "frost", direction, step_k
            return "hold", level
        if abs(after) >= abs(self.error_settled) - margin and not (
                (direction < 0 and self.error_now > URGENT_WARM_K)
                or (direction > 0 and (self.frost_cold or persistent_cold))):
            self.gate, self.gate_dir, self.gate_step_k = "step", direction, step_k
            return "hold", level
        if self._unholdable(inp, level + direction) and not self.urgent and self._acceptable(inp, self.error_settled):
            self.gate, self.gate_dir = "unholdable", direction
            return "hold", level
        # The dwell: a level is kept a while before a step that is not urgent. A
        # small step (no more than the band) on in the same direction only waits
        # for the last one to show (below); it cannot make the air swing.
        small_onward = direction == self.last_step_dir and step_k <= self._band_setting(inp)
        if not self.urgent and not small_onward and self._dwell_left(inp) > 0:
            self.gate, self.gate_dir = "dwell", direction
            return "hold", level
        # Under the frost lock a step down always waits for the last one to show:
        # too warm is no harm there, but a step learned short (the first opening)
        # would only show its cold after the next one has been taken too.
        frost_down = inp.frost_lock and direction < 0
        if (direction == self.last_step_dir and self.last_step_at is not None
                and (frost_down or not self.fan_moved)
                and inp.now - self.last_step_at < DEAD_TIME_GUARD_S and (frost_down or self.error_now <= URGENT_WARM_K)):
            # Even when urgent: the last step's response has not begun to show yet
            # (after a fan change the model knows where the air is going).
            self.gate, self.gate_dir = "wait", direction
            return "hold", level
        reversal_ok = (self.last_step_at is None or self.urgent or abs(self.error or 0.0) >= REVERSAL_URGENT_K
                       or inp.now - self.last_step_at >= REVERSAL_DWELL_TICKS * self.clock.period - DECIDE_BEFORE_S - 5)
        if direction > 0 and (self.last_step_dir >= 0 or reversal_ok):
            return "up", target
        if direction < 0 and (self.last_step_dir <= 0 or reversal_ok):
            return "down", target
        self.gate, self.gate_dir = "dwell", direction
        return "hold", target

    def _hold_setpoint(self, inp: PilotInputs, low: int, guesses: tuple[float, ...], floor_sp: int) -> int:
        """Whole-degree setpoint that makes HAC1 hold its level at the next tick.

        HAC1 holds ``sp`` for T2AH in [sp, sp + 0.95). The air at the tick is
        known only roughly (now, by its trend, by the model; HAC1's own reading
        scatters), and just below a whole degree nothing holds for sure. Each
        candidate may make HAC1 step up (reads below it) or down (reads 0.95 K
        above it); the one with the least expected harm - how likely the step
        is, times how much further from the target it would leave the air, plus
        the cost of an unasked step - is asked for, the current one unless
        another is clearly better.
        """
        def likely(z: float) -> float:
            return 0.5 * (1 + math.erf(z / math.sqrt(2)))
        now = guesses[0]  # the air now; at the tick by its trend and by the model
        middle = (max(guesses) + min(guesses)) / 2
        spread = max(HOLD_NOISE_K, (max(guesses) - min(guesses)) / 2)
        if self.pwm_low is not None:
            # Taking turns, the air moves fast all the time: the measured trend
            # is the best guess (the air now is already behind).
            middle = guesses[1]
            spread = max(PWM_GUESS_K, abs(guesses[1] - guesses[2]) / 2)
        stay = abs(self.error_settled)
        # Any step HAC1 takes by itself costs calm, even one that would help.
        harm_up = max(0.0, self._step_cost(inp, low, 1) - stay) + UNASKED_STEP_K
        down_cost = self._step_cost(inp, low, -1)
        harm_down = max(0.0, down_cost - stay) + UNASKED_STEP_K
        if self.pwm_low is not None:
            # Taking turns, the air is on its way and does not settle: a step
            # HAC1 takes by itself is taken back at the next tick, so what it
            # costs is the heat of that step for a tick (the two levels of the
            # pair are far apart; a step out of the pair is usually the small one).
            level = int(inp.level)
            scale = self.scale or 1.0
            harm_up = UNASKED_STEP_K + (0.0 if level >= LEVEL_MAX else scale * self.heat.step(level))
            harm_down = (STEP_COST_FORBIDDEN if level <= low
                         else UNASKED_STEP_K + scale * self.heat.step(level - 1))

        def harm(candidate: int) -> float:
            return (likely((candidate - middle) / spread) * harm_up
                    + likely((middle - candidate - HAC1_DOWN_BAND_K) / spread) * harm_down)
        forbidden = down_cost >= STEP_COST_FORBIDDEN or harm_down >= STEP_COST_FORBIDDEN
        if forbidden and self.fan_moved and self.pwm_low is None:
            # Right after a fan change the guesses lag where the air is going:
            # with a forbidden step down at stake the level's settled air (the
            # model knows it) is one of them.
            guesses = (*guesses, float(inp.target) + self.error_settled)
            middle = (max(guesses) + min(guesses)) / 2
            spread = max(HOLD_NOISE_K, (max(guesses) - min(guesses)) / 2)
        whole = math.floor(middle)
        candidates = [c for c in (whole - 1, whole, whole + 1) if c >= floor_sp] or [floor_sp]
        if middle > float(inp.target) + URGENT_WARM_K:
            # Far too warm: never a setpoint above the air, which makes HAC1 step up
            # (live 2026-11-20: sp 27 at 26.96 C with the target 21).
            candidates = [c for c in candidates if c <= middle] or candidates
        restricted = False
        if forbidden:
            # HAC1 must not step down here (the frost minimum level, or a level
            # that settles too close to the frost floor): a setpoint that holds
            # at the air expected (the frost floor's, or the whole degree below
            # the air) beats one that forces a step up. Only when none holds is
            # the least harmful one taken, which may force HAC1 up.
            holding = [c for c in candidates if c <= middle and max(guesses) + spread < c + HAC1_DOWN_BAND_K]
            if holding:
                candidates, restricted = holding, True
        # The air on its way through a whole degree to where this level settles:
        # ask for the range it is heading into, so that a step (if HAC1 takes
        # one) does not push it back to cross the same degree again.
        settled = float(inp.target) + self.error_settled
        moving = guesses[1] - now
        # (Not when a forbidden step down is all it would hold against: the harm decides then.)
        if (self.pwm_low is None and abs(moving) >= HOLD_MOVING_K and (settled - now) * moving > 0
                and (restricted or not forbidden)):
            for candidate in candidates:
                if (candidate + HOLD_SETTLED_MARGIN_K <= settled <= candidate + HAC1_DOWN_BAND_K - HOLD_SETTLED_MARGIN_K
                        and candidate - (1 - HAC1_DOWN_BAND_K) - spread / 2 <= middle < candidate + HAC1_DOWN_BAND_K + spread):
                    return candidate
        best = min(candidates, key=harm)
        if (self.setpoint is not None and self.setpoint >= floor_sp and self.setpoint_level == int(inp.level)
                and (not restricted or self.setpoint in candidates)
                and harm(self.setpoint) <= harm(best) + HOLD_GAP_HYSTERESIS):
            return self.setpoint
        return best

    def _step_cost(self, inp: PilotInputs, low: int, direction: int) -> float:
        """How far from the target the air settles (K) if HAC1 steps ``direction`` now."""
        level = int(inp.level)
        error = self.error_settled
        scale = self.scale or 1.0
        if direction > 0:
            return abs(error) if level >= LEVEL_MAX else abs(error + scale * self.heat.step(level))
        if level <= 0:
            return abs(error)
        if level <= low:
            return STEP_COST_FORBIDDEN  # the frost minimum level: HAC1 must not go lower
        if self._down_below_frost_floor(inp, level) and self._frost_blip_unsafe(inp, level):
            # Nor to a level that settles too close to the frost floor, when one
            # tick on it could take the air there. The hold setpoint still never
            # forces a step up for it while a setpoint that holds exists (see
            # _hold_setpoint). Further up such a step is a blip that the frost
            # revert (_plan: up at the next tick) takes back: a normal step.
            return STEP_COST_FORBIDDEN
        return abs(error - scale * self.heat.step(level - 1))

    def _choose_setpoint(self, inp: PilotInputs, action: str, wanted: float, low: int,
                         ahead: float = 0.0) -> tuple[str, int, str | None, float, str]:
        """Integer setpoint for ``action``; may change the action when it cannot be had.

        Judged on HAC1's raw T2AH (glitch-robust) as the model expects it ``ahead``
        seconds on. Also returns the HAC1 rule the setpoint relies on (a hold at
        level 0 is asked for as "down", which HAC1 cannot do there).
        """
        temperature = now_temperature = modelled = self._raw(inp)
        if ahead > 0:
            temperature += self._drift(inp.now, ahead)
            # What the model expects from the levels already on their way (e.g. a step HAC1 just undid).
            modelled += self.kelvin_per_heat * (self._lagged_at(inp.now, ahead) - self.heat_lagged)
        floor_sp = max(SETPOINT_MIN, math.ceil(inp.frost_floor) if inp.frost_lock else SETPOINT_MIN)
        level = int(inp.level)
        note = None
        if action == "hold" and level <= 0 and low <= 0:
            rule = "down"  # HAC1 cannot go below 0: any low setpoint holds, and keeps holding as the air drifts
        elif action == "hold" and level >= LEVEL_MAX:
            rule = "up"
        else:
            rule = action
        lean = wanted + self.sigma - level
        if lean == 0:
            lean = self.control_target - temperature
        just_above = temperature - math.floor(temperature) < HOLD_FRACTION_MIN
        if rule == "hold":
            # Calm before precision: a hold is always asked for, never a neighbour
            # step; the setpoint least likely to make HAC1 step (weighed by the harm).
            sp = self._hold_setpoint(inp, low, (now_temperature, temperature, modelled), floor_sp)
            if sp >= floor_sp:
                return "hold", int(_clamp(sp, SETPOINT_MIN, SETPOINT_MAX)), note, temperature, "hold"
        if rule == "hold" and (hac1_setpoint("hold", temperature) is None or (just_above and lean < 0)):
            # No whole degree holds safely: take the neighbour step the controller leans to.
            action = rule = "up" if lean > 0 else "down"
            if action == "down" and level <= low:
                action = rule = "up"
            note = "Intet helt setpunkt holder trinnet; tager nabotrinnet"
        if rule == "down":
            sp = hac1_setpoint("down", temperature)
            if sp < floor_sp:
                # Under the frost lock the setpoint cannot go low enough for a
                # safe step down: the frost floor's setpoint, the lowest allowed,
                # is sent. HAC1 then steps down or holds, both allowed (_plan only
                # asks for a level that settles clear of the floor). Never a
                # higher hold setpoint (just above a whole degree it makes HAC1
                # step up) and never a step up when a step down was wanted.
                rule = hac1_action(floor_sp, temperature)
                if rule != "down" and action == "down":
                    action, note = rule, "Frostsikring: setpunktet kan ikke gå lavere"
                return action, int(_clamp(floor_sp, SETPOINT_MIN, SETPOINT_MAX)), note, temperature, rule
        if self.setpoint is not None and self.setpoint >= floor_sp and setpoint_gives(self.setpoint, rule, temperature):
            return action, self.setpoint, note, temperature, rule  # fewest setpoint changes
        if rule == "down" and action == "hold":
            sp = hac1_setpoint("down", temperature) - 1  # a degree of room: fewer changes as the air drifts
            sp = max(sp, floor_sp)
            if hac1_action(sp, temperature) == "down":
                return action, int(_clamp(sp, SETPOINT_MIN, SETPOINT_MAX)), note, temperature, rule
        sp = hac1_setpoint(rule, temperature)
        if sp is None:
            sp = math.floor(temperature) + 1
            action = rule = "up"
        return action, int(_clamp(max(sp, floor_sp), SETPOINT_MIN, SETPOINT_MAX)), note, temperature, rule

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
        self.setpoint_level = int(inp.level)
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
        action, setpoint, note, temperature, rule = self._choose_setpoint(inp, action, wanted, low, ahead)
        if (setpoint != self.setpoint and self.setpoint is not None and len(self.changes) >= RATE_LIMIT_CHANGES
                and rule != "hold"
                and not ((self.urgent or self.pwm_low is not None or self.frost_up) and action != "hold")):
            # (A hold setpoint already changes only for a clearly better bet, see _hold_setpoint.)
            # Rate limit: keep the setpoint unless it would make HAC1 step the wrong way (or the air is far off).
            current = hac1_action(self.setpoint, temperature)
            floor_sp = math.ceil(inp.frost_floor) if inp.frost_lock else SETPOINT_MIN
            if (int(inp.level) >= low and self.setpoint >= floor_sp
                    and (current == "hold" or current == rule)):
                # A hold asked for as "down" at level 0 stays a hold.
                action = "hold" if action == "hold" else current
                rule, setpoint = current, self.setpoint
                note = "Begrænser ændringer af HAC1-setpunktet"
        self.margin = setpoint_margin(setpoint, rule, temperature)
        changed_action = action != self.action
        self.action, self.level_target = action, target
        self.band_hold = self.gate == "band" and action == "hold"
        if action in ("up", "down"):
            self.commanded_dir, self.commanded_at = (1 if action == "up" else -1), inp.now
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
        fan = _clamp(_num(inp.fan_supply) or FAN_REFERENCE_PERCENT, 15.0, 100.0)
        self.level_history = deque([(inp.now - MODEL_DEAD_TIME_S - 30, level, FAN_REFERENCE_PERCENT / fan)])
        self.heat_lagged = self.heat_fast = self.heat.heat(level)  # assume T2AH has settled on the level it has
        self.kelvin_lagged = self.heat_lagged * FAN_REFERENCE_PERCENT / fan
        self.heat_command = None
        self.kp, self.error = None, None
        self.integral = self.heat.heat(max(level, low))  # bumpless: u starts at the level
        self.last_fan, self.fan_changed_at, self.water_filtered, self.scale = None, None, None, None
        self.episodes, self.level_change_at, self.level_change_dir = [], None, 0
        self.raw_history.clear()
        self.sigma, self.follow_failures, self.margin = 0.0, 0, 0.0
        self.action, self.decided_for, self.check_at = None, None, None
        self.last_step_dir, self.last_step_at = 0, None
        self.commanded_dir, self.commanded_at = 0, None
        self.revert_level = None
        self.blips.clear()
        self.unholdable = {}
        self.gate, self.band_hold, self.in_band, self.urgent = None, False, False, False
        self.cold_since, self.frost_cold = None, False
        self._end_pwm()
        self.command_level, self.note = level, None
        self.active = True

    def _deactivate(self) -> None:
        self.active = False
        self.kp = None
        self.wanted = None
        self.episodes = []
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
            self.target_raised_from = None
            state = "native" if inp.mode != "pi" else "fallback"
            self.result = self._snapshot(inp, state, None, blocker)
            return self.result
        effective = self.effective_target(inp)
        self.target_raised_from = float(inp.target) if effective != float(inp.target) else None
        if self.target_raised_from is not None:
            inp = replace(inp, target=effective)  # everything below (and the snapshot) steers for it
        low = self.frost_min_level(inp)
        high = LEVEL_MAX
        self.control_target = hold_friendly(float(inp.target))
        if not self.active:
            self._activate(inp, low)
        self._check_follow(inp)
        if self.active and self.follow_failures >= FOLLOW_FAIL_LIMIT:
            return self.update(inp)  # falls back on the same call
        wanted = self._pi(inp, dt, low, high)
        level = int(inp.level)
        if self.pwm_low is not None:
            limit = PWM_DEBT_LIMIT * PWM_SWITCH_KS
            self.pwm_debt = _clamp(self.pwm_debt + self.error_now * dt, -limit, limit)
        held = self.action == "hold" and self.gate in HELD_GATES
        if self.in_band or held or self.pwm_low is not None:
            # A held level is a blocked actuator: the quantizer's sum decays instead of saturating.
            self.sigma *= math.exp(-dt / self.clock.period)
        elif abs(wanted - level) <= 1.0:
            self.sigma = _clamp(self.sigma + (wanted - level) * dt / self.clock.period, -1.0, 1.0)
        else:
            self.sigma *= math.exp(-dt / self.clock.period)
        self.wanted = wanted
        next_tick = self.clock.next_tick(now) if self.clock.known(now) else None
        if next_tick is None:
            # State based (valid at any moment), but a step asked for is kept for a
            # tick unless HAC1 took it: HAC1 may tick at any time.
            committed = (self.action in ("up", "down") and self.commanded_at is not None
                         and now - self.commanded_at < self.clock.period + 5 and self.command_level == level)
            if not committed:
                self._decide(inp, wanted, low, high, None)
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
        reason = self._plain_reason(inp)
        if self.target_raised_from is not None and inp.frost_lock:
            raised = f"Frostsikring: målet er hævet til {_danish(float(inp.target))} °C"
            return f"{raised} · {reason.removeprefix('Frostsikring: ')}"
        return reason

    def _plain_reason(self, inp: PilotInputs) -> str:
        level = int(inp.level)
        temperature = float(inp.t2ah)
        target = float(inp.target)
        low = self.frost_min_level(inp)
        if inp.frost_lock and level < low:
            return "Frostsikring: mindst trin 1"
        if self.note and inp.now - self.note_at <= 2 * self.clock.period and self.note.startswith(("HAC1 sommerspærre", "Trin ", "HAC1 tog", "HAC1 fulgte", "Frostsikring: HAC1")):
            return self.note
        air, goal = _danish(temperature), _danish(target)
        band = _danish(self._band_setting(inp))
        where = f"luft {air} °C (mål {goal} ±{band})"
        if self.frost_cold:
            floor = _danish(float(inp.frost_floor))
            doing = "går et trin op" if self.action == "up" else f"trin {level}"
            return f"Frostsikring: luften er under {floor} °C · {doing} · {where}"
        if self.pwm_low is not None:
            pair = f"trin {self.pwm_low} og {self.pwm_low + 1}"
            if self.action in ("up", "down"):
                way = "op" if self.action == "up" else "ned"
                return f"Skifter mellem {pair} (intet trin rammer målet): et trin {way} · {where}"
            return f"Skifter mellem {pair}, så luften i snit rammer målet: holder trin {level} · {where}"
        if self.action in ("up", "down") and self.revert_level is not None:
            way = "op" if self.action == "up" else "ned"
            return f"Retter HAC1's eget trin: et trin {way} igen · {where}"
        if self.action in ("up", "down") and self.in_band and self.gate is None:
            way = "op" if self.action == "up" else "ned"
            return (f"Går et trin {way}: luften ligger på en hel grad, som HAC1 ikke kan holde · "
                    f"{where}")
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
        if self.gate == "band":
            return f"I komfortbåndet: holder trin {level} · {where}"
        if self.gate == "step":
            way = "op" if self.gate_dir > 0 else "ned"
            return f"Holder trin {level}: et trin {way} flytter ca. {_danish(self.gate_step_k)} K · {where}"
        if self.gate == "frost":
            return (f"Holder trin {level}: på trin {level - 1} kommer luften under "
                    f"{_danish(float(math.ceil(float(inp.frost_floor))))} °C, det laveste HAC1 kan holde "
                    f"med frostsikring · {where}")
        if self.gate == "unholdable":
            other = level + self.gate_dir
            return (f"Holder trin {level}: på trin {other} ligger luften på en hel grad, "
                    f"som HAC1 ikke kan holde · {where}")
        if self.gate == "wait":
            way = "op" if self.gate_dir > 0 else "ned"
            return f"Venter på at sidste trin slår igennem, så et trin {way} · {where}"
        if self.gate == "dwell":
            way = "op" if self.gate_dir > 0 else "ned"
            left = self._dwell_left(inp)
            wait = f" om ca. {max(1, math.ceil(left / 60))} min" if left > 0 else " ved næste tick"
            return f"Rolig drift: går et trin {way}{wait} · {where}"
        return f"Holder trin {level} · {where}"

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
            "afterheat_in_band": bool(active and self.in_band),
        }


__all__ = [
    "AfterheatPilot", "PilotInputs", "TickClock", "LevelHeat", "CONTROL_MODES", "LEVEL_MAX", "TICK_PERIOD_S",
    "PI_GAIN_DEFAULT", "PI_GAIN_RANGE", "PI_INTEGRAL_MINUTES_DEFAULT", "PI_INTEGRAL_MINUTES_RANGE",
    "PI_BAND_DEFAULT", "PI_BAND_RANGE", "PI_MIN_DWELL_TICKS_DEFAULT", "PI_MIN_DWELL_TICKS_RANGE",
    "hold_friendly", "robust_temperature", "hac1_action", "hac1_setpoint", "setpoint_gives", "setpoint_margin",
]
