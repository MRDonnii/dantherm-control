"""Plant simulator for the HAC1 water afterheat (HCH5 MK1), for the Pi afterheat controller tests.

Fitted to the supervised experiments in tests/data/hac1_exp_*.csv (2 s rows):
dead time ~60 s, first-order lag ~120 s, and a concave heat per level that
falls with the supply airflow (the owner throttled the water with ball
valves, so the top levels add little). The Calefa flow temperature swings
38-50 C over ~40 min and scales the heat per level. HAC1 itself is modelled
as measured: a fixed tick (60.17 s), one step per tick, decided on its own
raw T2AH against the whole-degree setpoint, with rare warm glitches on 205.

The valves were opened before the 25-minute Pi trial on 2026-11-20
(tests/data/hac1_pilot_trial_open_valves.csv, 5 s rows). Two plants stand
for that: OPENED_VALVES, a linear coil of ~1.5 K a level at 27 % fan and
~2.4 K at 17 % (what one level was taken to be worth), and TRIAL_VALVES,
fitted to the trial itself with the dynamics above: level 1 gave next to
nothing (~0.2 K) and level 2 ~6 K (a valve that only opens properly from
level 2), see ``replay_trial``.
"""
from __future__ import annotations

import csv
import math
import random
from collections import deque
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"
TICK_PERIOD_S = 60.17


def level_curve(level: float, curvature: float) -> float:
    """Heat of a level relative to level 1 (concave: the first levels give the most)."""
    if level <= 0:
        return 0.0
    if curvature <= 0:
        return float(level)
    return (1 - math.exp(-level / curvature)) / (1 - math.exp(-1 / curvature))


class Hac1Plant:
    """Air after the water coil plus HAC1's own step controller."""

    def __init__(self, *, gain: float = 0.8, curvature: float = 3.0, level_heat: list[float] | None = None,
                 base: float = 20.0,
                 dead_time: float = 60.0, tau: float = 120.0, fan: float = 27.0,
                 water_mean: float = 44.0, water_amp: float = 6.0, water_period: float = 2400.0,
                 noise: float = 0.03, seed: int = 1, tick_offset: float = 17.3,
                 period: float = TICK_PERIOD_S, level: int = 1, setpoint: float = 21.0,
                 start: float = 1_000_000.0, tau_down: float | None = None) -> None:
        self.gain, self.curvature, self.base = gain, curvature, base
        # Explicit heat of each level relative to level 1's (overrides the curvature).
        self.level_heat = level_heat
        self.dead_time, self.tau, self.fan = dead_time, tau, fan
        # The coil cools slower than it heats (live RLV-2 fit 2026-11-25: ~110 s up, ~240 s down).
        self.tau_down = tau if tau_down is None else tau_down
        self.water_mean, self.water_amp, self.water_period = water_mean, water_amp, water_period
        self.noise = noise
        self.random = random.Random(seed)
        self.period = period
        self.level = level
        self.setpoint = setpoint
        self.now = start
        self.next_tick = start + tick_offset
        self.glitch_until = -1.0
        self.glitch_at_ticks: set[int] = set()
        self.tick_index = 0
        self.ignore_setpoint = False
        self.lockout = False
        self.history: deque[tuple[float, float]] = deque()
        self.x = self.steady(level)
        self.raw = self.x

    def water(self, t: float | None = None) -> float:
        t = self.now if t is None else t
        return self.water_mean + self.water_amp * math.sin(2 * math.pi * t / self.water_period)

    def air_in(self) -> float:
        # More air through the exchanger: a little colder before the coil.
        return self.base - 0.01 * (self.fan - 27.0)

    def rise(self, level: float, fan: float | None = None, water: float | None = None) -> float:
        fan = self.fan if fan is None else fan
        water = self.water() if water is None else water
        return self.gain * self.curve(level) * 27.0 / fan * max(0.0, water - 20.0) / 20.0

    def curve(self, level: float) -> float:
        if self.level_heat is None:
            return level_curve(level, self.curvature)
        return self.level_heat[int(max(0, min(10, level)))]

    def steady(self, level: float) -> float:
        return self.air_in() + self.rise(level)

    def step(self, dt: float = 1.0) -> None:
        self.now += dt
        self.history.append((self.now, self.steady(self.level)))
        while len(self.history) > 1 and self.history[1][0] <= self.now - self.dead_time:
            self.history.popleft()
        delayed = self.history[0][1]
        tau = self.tau if delayed >= self.x else self.tau_down
        self.x += (delayed - self.x) * (1 - math.exp(-dt / tau))
        self.raw = self.x + self.random.gauss(0.0, self.noise)
        if self.now < self.glitch_until:
            self.raw += 3.0
        if self.now >= self.next_tick:
            self._tick()
            self.next_tick += self.period

    def glitch(self, seconds: float = 6.0) -> None:
        """HAC1 reads 205 several K too warm for a few seconds, starting now."""
        self.glitch_until = self.now + seconds
        self.raw += 3.0

    def _tick(self) -> None:
        self.tick_index += 1
        if self.tick_index in self.glitch_at_ticks:
            self.raw = self.x + 3.0
        if self.ignore_setpoint or self.lockout:
            if self.lockout:
                self.level = 0
            return
        sp = math.floor(self.setpoint)
        if self.raw < sp:
            self.level = min(10, self.level + 1)
        elif self.raw >= sp + 0.95:
            self.level = max(0, self.level - 1)


class GatewayView:
    """What the gateway sees: polls every ~3 s, 15 s window minimum on T2AH."""

    def __init__(self, plant: Hac1Plant, poll: float = 3.0) -> None:
        self.plant = plant
        self.poll = poll
        self.next_poll = plant.now
        self.level: int | None = None
        self.level_at: float | None = None
        self.level_changed_at: float | None = None
        self.raw_window: deque[tuple[float, float]] = deque()
        self.filtered: float | None = None
        self.raw_at: float | None = None
        self.frozen = False

    def step(self) -> None:
        if self.frozen or self.plant.now < self.next_poll:
            return
        self.next_poll = self.plant.now + self.poll
        now = self.plant.now
        if self.level != self.plant.level:
            self.level_changed_at = now
        self.level, self.level_at = self.plant.level, now
        self.raw_window.append((now, self.plant.raw))
        while self.raw_window and now - self.raw_window[0][0] > 15.0:
            self.raw_window.popleft()
        self.filtered = min(value for _at, value in self.raw_window)
        self.raw_at = now

    def robust_raw(self, now: float) -> float | None:
        from afterheat_pilot import robust_temperature
        return robust_temperature([value for at, value in self.raw_window if now - at <= 10.0])


# Opened valves as one level was taken to be worth: ~1.5 K at 27 % fan, ~2.4 K at 17 %
# (water 44 C: 1.25 * 1.2 K a level at 27 %).
OPENED_VALVES = dict(gain=1.25, curvature=0.0, base=20.0)
# Fitted to the 2026-11-20 trial (see replay_trial): level 1 ~0.2 K, level 2 ~6.3 K at 27 %
# fan, then flattening (assumed; the trial never went above level 2).
TRIAL_LEVEL_HEAT = [0.0, 0.15, 5.25, 6.0, 6.6, 7.0, 7.3, 7.55, 7.75, 7.9, 8.0]
TRIAL_VALVES = dict(gain=1.0, level_heat=TRIAL_LEVEL_HEAT, base=20.5)
# The RLV balancing valve at 2 turns (live 2026-11-24/25 at 36 % fan): level 1 lifts the
# air ~2.4 K over the air before the coil (17.8 C), level 2 ~8.6 K, the rest flattening;
# the coil heats in ~110 s and cools in ~240 s. (Lifts / 0.9: 27/36 fan * 24/20 water.)
RLV2_LIFT_AT_36 = [0.0, 2.4, 8.6, 9.6, 10.3, 10.8, 11.2, 11.5, 11.7, 11.9, 12.0]
RLV2_VALVES = dict(gain=1.0, level_heat=[lift / 0.9 for lift in RLV2_LIFT_AT_36], base=17.89, fan=36.0,
                   tau=110.0, tau_down=240.0)


def run(pilot, plant: Hac1Plant, *, target: float, minutes: float, events=None,
        frost_lock: bool = False, frost_floor: float = 18.0, gain: float | None = None,
        integral_minutes: float | None = None, band: float | None = None,
        min_dwell_ticks: int | None = None, update_every: float = 2.0, inputs_hook=None,
        algorithm: str = "standard", sensors: bool = False):
    """Close the loop for ``minutes``; returns one row per pilot update.

    ``events`` maps seconds since start to a callable(plant, gateway) run once.
    ``algorithm`` picks the selectable algorithm (afterheat_actuators.AlgorithmPilot);
    ``sensors`` also gives the pilot the water flow and the air before the coil.
    """
    from afterheat_pilot import (PI_BAND_DEFAULT, PI_GAIN_DEFAULT, PI_INTEGRAL_MINUTES_DEFAULT,
                                 PI_MIN_DWELL_TICKS_DEFAULT, PilotInputs)

    gateway = GatewayView(plant)
    gateway.step()
    start = plant.now
    events = dict(events or {})
    rows = []
    next_update = plant.now
    while plant.now - start < minutes * 60:
        plant.step(1.0)
        gateway.step()
        elapsed = plant.now - start
        for at in [at for at in events if at <= elapsed]:
            events.pop(at)(plant, gateway)
        if plant.now < next_update:
            continue
        next_update = plant.now + update_every
        now = plant.now
        inp = PilotInputs(
            now=now, mode="pi", afterheat_on=True, target=target,
            frost_lock=frost_lock, frost_floor=frost_floor,
            level=gateway.level, level_age=None if gateway.level_at is None else now - gateway.level_at,
            level_changed_at=gateway.level_changed_at,
            t2ah=gateway.filtered, t2ah_age=None if gateway.raw_at is None else now - gateway.raw_at,
            t2ah_raw=gateway.robust_raw(now), fan_supply=plant.fan,
            water_flow=plant.water() if sensors else None, before_heater=plant.air_in() if sensors else None,
            outdoor=5.0, extract=21.0, master=True,
            gain=PI_GAIN_DEFAULT if gain is None else gain,
            integral_minutes=PI_INTEGRAL_MINUTES_DEFAULT if integral_minutes is None else integral_minutes,
            band=PI_BAND_DEFAULT if band is None else band,
            min_dwell_ticks=PI_MIN_DWELL_TICKS_DEFAULT if min_dwell_ticks is None else min_dwell_ticks,
            algorithm=algorithm,
        )
        if inputs_hook is not None:
            inputs_hook(inp, plant, gateway)
        result = pilot.update(inp)
        sp = result["afterheat_hac1_setpoint"]
        plant.setpoint = float(sp) if sp is not None else float(round(target) if not frost_lock else max(round(target), math.ceil(frost_floor)))
        rows.append({
            "t": elapsed, "level": plant.level, "air": plant.x, "filtered": gateway.filtered,
            "setpoint": plant.setpoint, "state": result["afterheat_control_active"],
            "wanted": result["afterheat_level_wanted"], "action": result["afterheat_next_action"],
            "reason": result["afterheat_control_reason"], "changes": result["afterheat_setpoint_changes_10min"],
            "fan": plant.fan,
        })
    return rows


class DirectValvePlant(Hac1Plant):
    """The same coil with a stepless 0-10 V valve (0-100 %) set directly; no HAC1 in between.

    The lift is interpolated between the whole-level values (a valve opening of 37 % lifts
    like level 3.7), the output is applied at once and kept until changed.
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.ignore_setpoint = True
        self.level = float(self.level)

    def curve(self, level: float) -> float:
        level = max(0.0, min(10.0, float(level)))
        if self.level_heat is None:
            return level_curve(level, self.curvature)
        whole = min(9, int(level))
        return self.level_heat[whole] + (level - whole) * (self.level_heat[whole + 1] - self.level_heat[whole])


def run_direct(controller, plant: DirectValvePlant, *, target: float, minutes: float, events=None,
               frost_lock: bool = False, frost_floor: float = 18.0, sensors: bool = False,
               update_every: float = 2.0):
    """Close the loop of a DirectValveController on a DirectValvePlant; one row per update."""
    start = plant.now
    events = dict(events or {})
    rows, raw = [], deque()
    next_update = plant.now
    while plant.now - start < minutes * 60:
        plant.step(1.0)
        raw.append((plant.now, plant.raw))
        while raw and plant.now - raw[0][0] > 30.0:
            raw.popleft()
        elapsed = plant.now - start
        for at in [at for at in events if at <= elapsed]:
            events.pop(at)(plant, None)
        if plant.now < next_update:
            continue
        next_update = plant.now + update_every
        from afterheat_pilot import robust_temperature
        air = robust_temperature([v for t, v in raw if plant.now - t <= 10.0])
        result = controller.update(now=plant.now, air=air, output=plant.level * 10.0, target=target,
                                   fan=plant.fan, water=plant.water() if sensors else None,
                                   air_in=plant.air_in() if sensors else None,
                                   frost_lock=frost_lock, frost_floor=frost_floor)
        plant.level = result["output_percent"] / 10.0
        rows.append({"t": elapsed, "level": plant.level, "air": plant.x, "wanted": result["wanted_percent"] / 10.0,
                     "override": result["override"], "reason": result["why"], "fan": plant.fan})
    return rows


def quality(rows, target: float, *, skip_minutes: float = 15.0) -> dict[str, float]:
    """Mean error, mean absolute error, lowest air, share under target - 0.5 K, output changes an hour."""
    kept = [r for r in rows if r["t"] >= skip_minutes * 60]
    errors = [r["air"] - target for r in kept]
    hours = (kept[-1]["t"] - kept[0]["t"]) / 3600 if len(kept) > 1 else 0.0
    changes = sum(1 for a, b in zip(kept, kept[1:]) if a["level"] != b["level"])
    return {"mean": sum(errors) / len(errors), "mae": sum(abs(e) for e in errors) / len(errors),
            "lowest": min(r["air"] for r in kept), "under": 100 * sum(e < -0.5 for e in errors) / len(errors),
            "changes_h": changes / hours if hours else 0.0}


def load_experiment(name: str) -> list[dict[str, float]]:
    with (DATA / name).open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    t0 = float(rows[0]["time"])
    return [{"t": float(r["time"]) - t0, "level": int(r["level"]), "t2ah": float(r["t2ah"]),
             "raw": float(r["t2ah_raw"]), "fan": float(r["fan_supply"])} for r in rows]


def replay_rms(rows, *, gain: float = 0.8, curvature: float = 3.0, dead_time: float = 60.0,
               tau: float = 120.0, period: float = 2400.0) -> tuple[float, float]:
    """RMS error of the model on a logged level sequence.

    The unknown air temperature before the coil and the Calefa swing are
    fitted as offset + one 40 min sinusoid (least squares). Returns (rms, amplitude).
    """
    model = []
    x = None
    for i, row in enumerate(rows):
        delayed_t = row["t"] - dead_time
        j = max(0, max((k for k in range(i + 1) if rows[k]["t"] <= delayed_t), default=0))
        u = gain * level_curve(rows[j]["level"], curvature) * 27.0 / rows[j]["fan"]
        if x is None:
            x = u
        else:
            x += (u - x) * (1 - math.exp(-(row["t"] - rows[i - 1]["t"]) / tau))
        model.append(x)
    basis = [(1.0, math.sin(2 * math.pi * r["t"] / period), math.cos(2 * math.pi * r["t"] / period)) for r in rows]
    residual = [r["t2ah"] - m for r, m in zip(rows, model)]
    ata = [[sum(b[i] * b[j] for b in basis) for j in range(3)] for i in range(3)]
    atb = [sum(b[i] * y for b, y in zip(basis, residual)) for i in range(3)]
    coef = _solve3(ata, atb)
    errors = [y - sum(c * v for c, v in zip(coef, b)) for b, y in zip(basis, residual)]
    return math.sqrt(sum(e * e for e in errors) / len(errors)), math.hypot(coef[1], coef[2])


def _solve3(a, b):
    m = [row[:] + [value] for row, value in zip(a, b)]
    for col in range(3):
        pivot = max(range(col, 3), key=lambda r: abs(m[r][col]))
        m[col], m[pivot] = m[pivot], m[col]
        for r in range(3):
            if r != col:
                factor = m[r][col] / m[col][col]
                m[r] = [x - factor * y for x, y in zip(m[r], m[col])]
    return [m[i][3] / m[i][i] for i in range(3)]


def load_trial(name: str = "hac1_pilot_trial_open_valves.csv") -> list[dict[str, float]]:
    """The Pi trial log (5 s rows, relative time): level, filtered T2AH and supply fan."""
    with (DATA / name).open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    return [{"t": float(r["time"]), "level": int(r["afterheat_level"]),
             "t2ah": float(r["actual_supply_air_temperature"]), "fan": float(r["actual_fan_supply_percent"]),
             "wanted": float(r["afterheat_level_wanted"] or 0), "setpoint": float(r["afterheat_hac1_setpoint"]),
             "reason": r["afterheat_control_reason"]} for r in rows]


def replay_trial(rows, level_heat, *, gain: float = 1.0, water: float = 44.0, dead_time: float = 60.0,
                 tau: float = 120.0) -> float:
    """RMS error of a plant on the logged trial levels.

    The air before the coil drifted during the trial; it is fitted as a
    quadratic in time (least squares). The fan moved only once, for one row.
    """
    model = []
    x = None
    for i, row in enumerate(rows):
        delayed_t = row["t"] - dead_time
        j = max((k for k in range(i + 1) if rows[k]["t"] <= delayed_t), default=0)
        u = gain * level_heat[rows[j]["level"]] * 27.0 / rows[j]["fan"] * (water - 20.0) / 20.0
        x = u if x is None else x + (u - x) * (1 - math.exp(-(row["t"] - rows[i - 1]["t"]) / tau))
        model.append(x)
    basis = [(1.0, r["t"] / 1000, (r["t"] / 1000) ** 2) for r in rows]
    residual = [r["t2ah"] - m for r, m in zip(rows, model)]
    ata = [[sum(b[i] * b[j] for b in basis) for j in range(3)] for i in range(3)]
    atb = [sum(b[i] * y for b, y in zip(basis, residual)) for i in range(3)]
    coef = _solve3(ata, atb)
    errors = [y - sum(c * v for c, v in zip(coef, b)) for b, y in zip(basis, residual)]
    return math.sqrt(sum(e * e for e in errors) / len(errors))
