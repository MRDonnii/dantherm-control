"""Plant simulator for the HAC1 water afterheat (HCH5 MK1), for the Pi afterheat controller tests.

Fitted to the supervised experiments in tests/data/hac1_exp_*.csv (2 s rows):
dead time ~60 s, first-order lag ~120 s, and a concave heat per level that
falls with the supply airflow (the owner throttled the water with ball
valves, so the top levels add little). The Calefa flow temperature swings
38-50 C over ~40 min and scales the heat per level. HAC1 itself is modelled
as measured: a fixed tick (60.17 s), one step per tick, decided on its own
raw T2AH against the whole-degree setpoint, with rare warm glitches on 205.
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

    def __init__(self, *, gain: float = 0.8, curvature: float = 3.0, base: float = 20.0,
                 dead_time: float = 60.0, tau: float = 120.0, fan: float = 27.0,
                 water_mean: float = 44.0, water_amp: float = 6.0, water_period: float = 2400.0,
                 noise: float = 0.03, seed: int = 1, tick_offset: float = 17.3,
                 period: float = TICK_PERIOD_S, level: int = 1, setpoint: float = 21.0,
                 start: float = 1_000_000.0) -> None:
        self.gain, self.curvature, self.base = gain, curvature, base
        self.dead_time, self.tau, self.fan = dead_time, tau, fan
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
        return self.gain * level_curve(level, self.curvature) * 27.0 / fan * max(0.0, water - 20.0) / 20.0

    def steady(self, level: float) -> float:
        return self.air_in() + self.rise(level)

    def step(self, dt: float = 1.0) -> None:
        self.now += dt
        self.history.append((self.now, self.steady(self.level)))
        while len(self.history) > 1 and self.history[1][0] <= self.now - self.dead_time:
            self.history.popleft()
        delayed = self.history[0][1]
        self.x += (delayed - self.x) * (1 - math.exp(-dt / self.tau))
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


def run(pilot, plant: Hac1Plant, *, target: float, minutes: float, events=None,
        frost_lock: bool = False, frost_floor: float = 18.0, gain: float | None = None,
        integral_minutes: float | None = None, update_every: float = 2.0, inputs_hook=None):
    """Close the loop for ``minutes``; returns one row per pilot update.

    ``events`` maps seconds since start to a callable(plant, gateway) run once.
    """
    from afterheat_pilot import PI_GAIN_DEFAULT, PI_INTEGRAL_MINUTES_DEFAULT, PilotInputs

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
            t2ah_raw=gateway.robust_raw(now), fan_supply=plant.fan, water_flow=None,
            outdoor=5.0, extract=21.0, master=True,
            gain=PI_GAIN_DEFAULT if gain is None else gain,
            integral_minutes=PI_INTEGRAL_MINUTES_DEFAULT if integral_minutes is None else integral_minutes,
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
        })
    return rows


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
