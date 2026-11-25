"""Shared parts of the selectable afterheat algorithms: output spec, coil model, safety layer.

The afterheat is split in three layers (see afterheat_algorithms and afterheat_actuators):

* an **algorithm** says which output it wants next (a whole HAC1 level 0..10 today,
  or a valve opening 0-100 % with a stepless 0-10 V valve later); it sees only the
  allowed outputs and the move limit (``OutputSpec``) and the plant (``ControlView``);
* the **safety layer** (``SafetyLayer``) has the last word on every algorithm's
  choice: output limits, frost lock, soft floor, ping-pong guard and rate limit;
* the **actuator** gets the valve there: today through HAC1's whole-degree setpoint
  (at most one level per HAC1 tick), later a direct 0-10 V output.

Outputs are kept in level units everywhere (0..10, a stepless valve's 37 % is 3.7),
so one coil model (``CoilModel``: lift per output, asymmetric lag and dead time, a
Kalman offset, online learning and a holdout-gated re-identification) serves both.
Everything here is pure logic: the caller passes the time and the measurements.
"""
from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass, field

LOG = logging.getLogger("afterheat-control")

# The selectable algorithms (setting afterheat_algorithm), their codes in the history,
# and their plain Danish names with a one-line explanation for the WebUI.
ALGORITHMS = ("standard", "mpc", "mpc_simple", "pid", "tpi", "heat_power")
ALGORITHM_DEFAULT = "standard"
ALGORITHM_CODES = {name: index for index, name in enumerate(ALGORITHMS)}
ALGORITHM_LABELS = {
    "standard": "Standard (Pi-pilot)",
    "mpc": "Forudseende (MPC)",
    "mpc_simple": "Forudseende enkel",
    "pid": "Klassisk regulator (PID)",
    "tpi": "Tidsdeling (TPI)",
    "heat_power": "Varmeeffekt",
}
ALGORITHM_HELP = {
    "standard": "Den styring, der kører i dag: holder luften i et komfortbånd og skifter mellem to trin, når intet trin rammer målet",
    "mpc": "Regner 12 minutter frem og vælger det trin, der holder luften tættest på målet uden at blive for kold",
    "mpc_simple": "Regner 6 minutter frem med én simpel model og vælger ét trin ad gangen, hellere lidt for varmt end for koldt",
    "pid": "Retter efter hvor langt luften er fra målet, hvor længe den har været det, og hvor hurtigt den ændrer sig",
    "tpi": "Deler tiden mellem de to trin omkring målet i faste runder på 6 minutter",
    "heat_power": "Vælger trin ud fra hvor meget varme hvert trin giver lige nu, og justerer langsomt efter",
}

# Safety overrides (history column afterheat_safety_override); 0 = the algorithm's choice stood.
OVERRIDE_NONE, OVERRIDE_LIMIT, OVERRIDE_MOVE, OVERRIDE_PINGPONG, OVERRIDE_RATE, OVERRIDE_FLOOR, OVERRIDE_FROST, \
    OVERRIDE_FAULT = range(8)
OVERRIDE_TEXT = {
    OVERRIDE_LIMIT: "uden for det tilladte",
    OVERRIDE_MOVE: "højst ét skridt ad gangen",
    OVERRIDE_PINGPONG: "holder et par minutter efter skift frem og tilbage",
    OVERRIDE_RATE: "for mange skift de sidste 10 minutter",
    OVERRIDE_FLOOR: "luften er på vej under målet − 0,5 °C",
    OVERRIDE_FROST: "frostsikring",
    OVERRIDE_FAULT: "fejl i styringen; standard bruges",
}

LEVEL_MAX = 10
# The coil at the reference conditions: supply fan % and water minus air before the coil (K).
FAN_REF = 36.0
WATER_LIFT_REF = 26.0
SCALE_RANGE = (0.2, 5.0)
# Prior lift (K at the reference) per level and the prior dynamics: the live RLV-2 fit 2026-11-25
# (balancing valve at 2 turns, 36 % fan, water ~44 C over ~18 C air). Learned from there.
PRIOR_LIFT = (0.0, 2.4, 8.6, 9.6, 10.3, 10.8, 11.2, 11.5, 11.7, 11.9, 12.0)
TAU_UP_PRIOR_S, TAU_DOWN_PRIOR_S, DEAD_PRIOR_S = 110.0, 240.0, 60.0
TAU_RANGE_S = (40.0, 600.0)
DEAD_RANGE_S = (15.0, 150.0)
LIFT_MAX_K = 40.0
# Kalman filter on [air, offset]: process noise per second (K^2/s) and the sensor variance (K^2).
KF_Q_AIR = 2e-5
KF_Q_OFFSET = 1e-5
KF_R = 0.05 ** 2
KF_SUBSTEP_S = 15.0
OFFSET_LIMIT_K = 8.0
# Learning the lift per output from the offset, once the output has shown (dead time + this).
LEARN_AFTER_S = 45.0
LEARN_TIME_S = 300.0
# The lift of each output is also learned from transients (a short turn on a level never
# settles): normalised LMS on the model's sensitivity to each lift node, this slow.
LMS_TIME_S = 300.0
# The model's own score: the air predicted CHECK_AHEAD_S ahead against what came, last hour.
CHECK_AHEAD_S = 60.0
CHECK_MEMORY_S = 3600.0
# Holdout-gated re-identification of the lag and the dead time.
REID_EVERY_S = 30 * 60.0
REID_BUFFER_S = 3 * 3600.0
REID_SAMPLE_S = 10.0
REID_MIN_S = 60 * 60.0
REID_MIN_CHANGES = 4
REID_SEGMENT_S = 10 * 60.0
REID_HOLDOUT_SHARE = 0.25
REID_REL_GAIN = 0.10
REID_ABS_GAIN_K = 0.03

# Safety layer.
SOFT_FLOOR_K = 0.5
RATE_WINDOW_S = 600.0
RATE_LIMIT_CHANGES = 6
PINGPONG_CHANGES = 3
PINGPONG_HOLD_TICKS = 4
WARM_BIAS_K = 0.3
FROST_MARGIN_K = 0.2


def num(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def danish(value: float, digits: int = 1) -> str:
    return f"{value:.{digits}f}".replace(".", ",").replace("-", "−")


@dataclass(frozen=True)
class OutputSpec:
    """What an actuator can do: outputs low..high (level units) in steps of ``resolution``, at most
    ``max_move`` per decision, one decision per ``period`` s, shown ``apply_delay_s`` after it."""
    low: float = 0.0
    high: float = float(LEVEL_MAX)
    resolution: float = 1.0
    max_move: float = 1.0
    period: float = 60.17
    apply_delay_s: float = 15.0
    unit: str = "level"

    @property
    def discrete(self) -> bool:
        return self.resolution >= 1.0

    def snap(self, value: float) -> float:
        steps = round(value / self.resolution)
        return round(steps * self.resolution, 6)

    def limit(self, value: float, current: float, low: float, high: float) -> float:
        """``value`` within [low, high], at most max_move from ``current``, on the grid."""
        value = clamp(value, current - self.max_move, current + self.max_move)
        value = clamp(self.snap(value), low, high)
        return self.snap(value)

    def show(self, value: float) -> str:
        if self.discrete:
            return f"trin {int(round(value))}"
        return f"{int(round(value * 10))} %"


# HAC1 today: whole levels, one per tick (~60.17 s), decided ~15 s before the tick.
HAC1_SPEC = OutputSpec()
# A stepless 0-10 V valve driven directly by the Pi (not in hardware yet): 1 % steps,
# 40 % a minute (a typical 150 s actuator), applied at once.
DIRECT_SPEC = OutputSpec(resolution=0.1, max_move=4.0, period=60.0, apply_delay_s=0.0, unit="percent")


@dataclass
class ControlView:
    """One decision's look at the plant, the same for every algorithm."""
    now: float
    target: float
    air: float
    output: float
    low: float
    high: float
    spec: OutputSpec
    model: "CoilModel"
    fan: float = FAN_REF
    water: float | None = None
    air_in: float | None = None
    frost_lock: bool = False
    frost_floor: float = 18.0
    # Measured slope of the air (K/s), when known.
    trend: float | None = None


@dataclass
class Decision:
    output: float
    why: str
    # The plan behind it and the air it expects each minute (MPC), for the diagnostics.
    plan: tuple[float, ...] = ()
    forecast: tuple[float, ...] = ()


# ------------------------------------------------------------------ coil model
def interp(table: list[float], x: float) -> float:
    """Piecewise-linear on the whole-level nodes, linear beyond the ends."""
    if x <= 0:
        return x * (table[1] - table[0])
    if x >= LEVEL_MAX:
        return table[LEVEL_MAX] + (x - LEVEL_MAX) * (table[LEVEL_MAX] - table[LEVEL_MAX - 1])
    whole = int(x)
    return table[whole] + (x - whole) * (table[whole + 1] - table[whole])


def inverse(table: list[float], lift: float) -> float:
    """The output giving ``lift`` (inverse of ``interp``; the table is non-decreasing)."""
    if lift <= 0:
        return 0.0
    for whole in range(LEVEL_MAX):
        if lift <= table[whole + 1]:
            span = table[whole + 1] - table[whole]
            return whole + ((lift - table[whole]) / span if span > 1e-9 else 0.0)
    return float(LEVEL_MAX)


class CoilModel:
    """Air after the coil: lift per output (scaled by fan and water), dead time, asymmetric lag, offset.

        steady = air_in + scale * lift(output delayed by the dead time) + offset
        air   -> steady with tau_up (heating) or tau_down (cooling)

    The offset (K) is a random walk in a Kalman filter on [air, offset]; once an output has
    shown, the offset is moved slowly into that output's lift (online learning). Every
    REID_EVERY_S the lag, the dead time and the overall lift are re-fitted on the logged
    minutes and adopted only when they predict the newest (held-out) part clearly better.
    """

    VERSION = 1

    def __init__(self) -> None:
        self.lift = list(PRIOR_LIFT)
        self.tau_up, self.tau_down, self.dead = TAU_UP_PRIOR_S, TAU_DOWN_PRIOR_S, DEAD_PRIOR_S
        self.learned_s = 0.0
        self.reid_accepted = 0
        self.reid_tried = 0
        self.reid_last: dict[str, float] | None = None
        self.air_in_last: float | None = None
        self._reset_filter()
        self.outputs: deque[tuple[float, float]] = deque()
        self.buffer: deque[tuple[float, float, float, float, float]] = deque()
        self.last_sample: float | None = None
        self.reid_at: float | None = None
        self.seeded = False
        # Open-loop checks (due at, predicted air) and the errors seen (when, K).
        self.checks: deque[tuple[float, float]] = deque()
        self.check_errors: deque[tuple[float, float]] = deque()
        self.samples = 0
        self.last_scale = 1.0

    def _reset_filter(self) -> None:
        self.sens = [0.0] * (LEVEL_MAX + 1)
        self.x: float | None = None
        self.offset = 0.0
        self.p = [[1.0, 0.0], [0.0, 1.0]]
        self.updated: float | None = None

    # -------------------------------------------------------------- inputs
    @staticmethod
    def scale(fan: float | None, water: float | None, air_in: float | None) -> float:
        fan = clamp(num(fan) or FAN_REF, 10.0, 100.0)
        value = FAN_REF / fan
        water, air_in = num(water), num(air_in)
        if water is not None and air_in is not None and 5.0 <= water - air_in <= 80.0:
            value *= (water - air_in) / WATER_LIFT_REF
        return clamp(value, *SCALE_RANGE)

    def base(self, air_in: float | None) -> float:
        air_in = num(air_in)
        if air_in is not None and -30 < air_in < 50:
            return air_in
        return self.air_in_last if self.air_in_last is not None else 18.0

    def output_at(self, at: float) -> float:
        """The output in force at ``at`` (from the logged outputs)."""
        value = self.outputs[0][1] if self.outputs else 0.0
        for when, output in self.outputs:
            if when > at:
                break
            value = output
        return value

    def seed(self, lift: list[float]) -> None:
        """A first lift table (e.g. from the standard pilot's learned steps) while nothing is learned."""
        if self.learned_s > 0 or len(lift) != LEVEL_MAX + 1:
            return
        table, previous = [0.0], 0.0
        for value in lift[1:]:
            previous = clamp(max(previous, float(value)), 0.0, LIFT_MAX_K)
            table.append(previous)
        self.lift, self.seeded = table, True

    # --------------------------------------------------------- filter/learn
    def observe(self, now: float, air: float, output: float, fan: float | None, water: float | None,
                air_in: float | None) -> None:
        """One measurement (any rate): log the output, advance and correct the filter, learn."""
        if num(air) is None or num(output) is None:
            return
        if num(air_in) is not None and -30 < float(air_in) < 50:
            self.air_in_last = float(air_in)
        if not self.outputs or self.outputs[-1][1] != output:
            self.outputs.append((now, float(output)))
        while len(self.outputs) > 2 and self.outputs[1][0] < now - 2 * DEAD_RANGE_S[1]:
            self.outputs.popleft()
        scale = self.last_scale = self.scale(fan, water, air_in)
        base = self.base(air_in)
        if self.x is None or self.updated is None or now - self.updated > 600 or now < self.updated:
            self._reset_filter()
            self.x = air
            self.offset = clamp(air - base - scale * interp(self.lift, self.output_at(now - self.dead)),
                                -OFFSET_LIMIT_K, OFFSET_LIMIT_K)
            self.updated = now
        else:
            t = start = self.updated
            while t < now - 1e-6:
                dt = min(KF_SUBSTEP_S, now - t)
                self._predict(dt, base, scale, self.output_at(t + dt / 2 - self.dead))
                t += dt
            self.updated = now
            innovation = self._correct(air)
            self._lms(innovation, now - start)
            self._learn(now, now - start, scale)
        self._check(now, air, scale, base)
        self._log(now, air, output, scale, base)

    def _check(self, now: float, air: float, scale: float, base: float) -> None:
        """Score the model: the air it said CHECK_AHEAD_S ago against the air now."""
        while self.checks and self.checks[0][0] <= now + 1.0:
            due, predicted = self.checks.popleft()
            if now - due <= 10.0:
                self.check_errors.append((now, air - predicted))
        while self.check_errors and now - self.check_errors[0][0] > CHECK_MEMORY_S:
            self.check_errors.popleft()
        if not self.checks or now - (self.checks[-1][0] - CHECK_AHEAD_S) >= CHECK_AHEAD_S:
            if self.x is not None:
                path = self.simulate(now, [self.output_at(now)], OutputSpec(period=CHECK_AHEAD_S, apply_delay_s=0.0),
                                     scale, base, step_s=15.0)
                self.checks.append((now + CHECK_AHEAD_S, path[-1]))

    def prediction_error(self) -> float | None:
        """Root mean square (K) of the minute-ahead predictions over the last hour."""
        if len(self.check_errors) < 5:
            return None
        return math.sqrt(sum(e * e for _t, e in self.check_errors) / len(self.check_errors))

    def _predict(self, dt: float, base: float, scale: float, shown: float) -> None:
        steady = base + scale * interp(self.lift, shown) + self.offset
        tau = self.tau_up if steady >= self.x else self.tau_down
        a = math.exp(-dt / tau)
        self.x = a * self.x + (1 - a) * steady
        lower = int(clamp(math.floor(shown), 0, LEVEL_MAX - 1))
        fraction = clamp(shown - lower, 0.0, 1.0)
        for node in range(1, LEVEL_MAX + 1):
            weight = (1 - fraction if node == lower else 0.0) + (fraction if node == lower + 1 else 0.0)
            self.sens[node] = a * self.sens[node] + (1 - a) * scale * weight
        p = self.p
        # F = [[a, 1 - a], [0, 1]]
        p00 = a * a * p[0][0] + 2 * a * (1 - a) * p[0][1] + (1 - a) ** 2 * p[1][1] + KF_Q_AIR * dt
        p01 = a * p[0][1] + (1 - a) * p[1][1]
        p11 = p[1][1] + KF_Q_OFFSET * dt
        self.p = [[p00, p01], [p01, p11]]

    def _correct(self, air: float) -> float:
        p = self.p
        s = p[0][0] + KF_R
        k0, k1 = p[0][0] / s, p[0][1] / s
        innovation = air - self.x
        self.x += k0 * innovation
        self.offset = clamp(self.offset + k1 * innovation, -OFFSET_LIMIT_K, OFFSET_LIMIT_K)
        self.p = [[(1 - k0) * p[0][0], (1 - k0) * p[0][1]],
                  [(1 - k0) * p[0][1], p[1][1] - k1 * p[0][1]]]
        return innovation

    def _lms(self, innovation: float, dt: float) -> None:
        """Normalised LMS on the lift nodes the air is still feeling (also short turns)."""
        energy = sum(v * v for v in self.sens)
        if dt <= 0 or energy < 0.05:
            return
        gain = min(1.0, dt / LMS_TIME_S) * clamp(innovation, -2.0, 2.0) / (energy + 0.5)
        touched = None
        for node in range(1, LEVEL_MAX + 1):
            if self.sens[node] > 0.02:
                self.lift[node] = clamp(self.lift[node] + gain * self.sens[node], 0.0, LIFT_MAX_K)
                touched = node if touched is None else touched
        if touched is not None:
            self._monotone(touched)
        self.samples += 1

    def _learn(self, now: float, dt: float, scale: float) -> None:
        """Move the offset into the lift of the output that has shown for a while."""
        shown = self.output_at(now - self.dead)
        if shown <= 0.05 or dt <= 0:
            return
        if any(now - self.dead - LEARN_AFTER_S < when <= now for when, _value in self.outputs):
            return  # the output changed lately: the offset is still the transient's
        moved = min(1.0, dt / LEARN_TIME_S) * self.offset
        lower = int(clamp(math.floor(shown), 0, LEVEL_MAX - 1))
        fraction = shown - lower
        nodes = [(node, weight) for node, weight in ((lower, 1.0 - fraction), (lower + 1, fraction))
                 if node >= 1 and weight > 1e-9]
        for node, weight in nodes:
            self.lift[node] = clamp(self.lift[node] + weight * moved / scale, 0.0, LIFT_MAX_K)
        if nodes:
            self._monotone(nodes[0][0])
            self.offset -= moved
            self.learned_s += dt

    def _monotone(self, around: int) -> None:
        for node in range(1, LEVEL_MAX + 1):
            if node > around:
                self.lift[node] = max(self.lift[node], self.lift[node - 1])
        for node in range(around - 1, 0, -1):
            self.lift[node] = min(self.lift[node], self.lift[node + 1])
        self.lift[0] = 0.0

    # --------------------------------------------------------------- re-ID
    def _log(self, now: float, air: float, output: float, scale: float, base: float) -> None:
        if self.last_sample is not None and now - self.last_sample < REID_SAMPLE_S:
            return
        self.last_sample = now
        self.buffer.append((now, air, float(output), scale, base))
        while self.buffer and self.buffer[0][0] < now - REID_BUFFER_S:
            self.buffer.popleft()
        if self.reid_at is None:
            self.reid_at = now
        elif now - self.reid_at >= REID_EVERY_S:
            self.reid_at = now
            self.reidentify()

    def _segments(self, rows: list[tuple[float, float, float, float, float]]) -> list[list]:
        segments, current = [], []
        for row in rows:
            if current and (row[0] - current[-1][0] > 3 * REID_SAMPLE_S or row[0] - current[0][0] > REID_SEGMENT_S):
                if len(current) >= 12:
                    segments.append(current)
                current = []
            current.append(row)
        if len(current) >= 12:
            segments.append(current)
        return segments

    def _rmse(self, segments: list[list], tau_up: float, tau_down: float, dead: float, gain: float) -> float | None:
        """Free-run error (K) of the model on ``segments``, each with its own constant offset."""
        total, count = 0.0, 0
        for segment in segments:
            times = [row[0] for row in segment]
            x = segment[0][1]
            offset = x - segment[0][4] - segment[0][3] * gain * interp(self.lift, segment[0][2])
            residuals = []
            shown = 0
            for i in range(1, len(segment)):
                row = segment[i]
                while shown + 1 < i and times[shown + 1] <= row[0] - dead:
                    shown += 1  # the output logged at or before row time - dead time
                dt = row[0] - times[i - 1]
                steady = row[4] + row[3] * gain * interp(self.lift, segment[shown][2]) + offset
                tau = tau_up if steady >= x else tau_down
                x += (steady - x) * (1 - math.exp(-dt / tau))
                residuals.append(row[1] - x)
            if len(residuals) >= 3:
                # The unmeasured drift (e.g. the water's swing) as a straight line per segment.
                n = len(residuals)
                xs = range(n)
                mx, my = (n - 1) / 2, sum(residuals) / n
                den = sum((x - mx) ** 2 for x in xs)
                slope = sum((x - mx) * (r - my) for x, r in zip(xs, residuals)) / den if den else 0.0
                total += sum((r - my - slope * (x - mx)) ** 2 for x, r in zip(xs, residuals))
                count += n
        return math.sqrt(total / count) if count else None

    def reidentify(self) -> dict[str, float] | None:
        """Fit the lags and the dead time on the training minutes; adopt only on a clearly better holdout."""
        rows = list(self.buffer)
        if len(rows) < 2 or rows[-1][0] - rows[0][0] < REID_MIN_S:
            return None
        changes = sum(1 for a, b in zip(rows, rows[1:]) if a[2] != b[2])
        split = rows[-1][0] - (rows[-1][0] - rows[0][0]) * REID_HOLDOUT_SHARE
        training = self._segments([row for row in rows if row[0] < split])
        holdout = self._segments([row for row in rows if row[0] >= split])
        holdout_changes = sum(1 for segment in holdout for a, b in zip(segment, segment[1:]) if a[2] != b[2])
        if changes < REID_MIN_CHANGES or not training or not holdout or holdout_changes < 1:
            return None
        self.reid_tried += 1
        params = {"tau_up": self.tau_up, "tau_down": self.tau_down, "dead": self.dead, "gain": 1.0}
        ranges = {"tau_up": TAU_RANGE_S, "tau_down": TAU_RANGE_S, "dead": DEAD_RANGE_S, "gain": (0.5, 2.0)}
        factors = (0.7, 0.85, 1.0, 1.18, 1.4)

        def fit(p: dict[str, float], segments: list[list]) -> float:
            value = self._rmse(segments, p["tau_up"], p["tau_down"], p["dead"], p["gain"])
            return math.inf if value is None else value

        best = fit(params, training)
        for _round in range(2):
            # (The lift itself is learned online per output; a common factor here would undo that.)
            for key in ("tau_up", "tau_down", "dead"):
                for factor in factors:
                    trial = dict(params)
                    trial[key] = clamp(params[key] * factor, *ranges[key])
                    value = fit(trial, training)
                    if value < best - 1e-6:
                        best, params = value, trial
        prior = fit({"tau_up": self.tau_up, "tau_down": self.tau_down, "dead": self.dead, "gain": 1.0}, holdout)
        fitted = fit(params, holdout)
        accepted = (math.isfinite(prior) and math.isfinite(fitted) and fitted <= (1 - REID_REL_GAIN) * prior
                    and prior - fitted >= REID_ABS_GAIN_K)
        self.reid_last = {"at": rows[-1][0], "prior_rmse": round(prior, 3), "fitted_rmse": round(fitted, 3),
                          "accepted": 1.0 if accepted else 0.0, **{k: round(v, 3) for k, v in params.items()}}
        if accepted:
            self.tau_up, self.tau_down, self.dead = params["tau_up"], params["tau_down"], params["dead"]
            self.lift = [clamp(value * params["gain"], 0.0, LIFT_MAX_K) for value in self.lift]
            self.reid_accepted += 1
            LOG.info("Afterheat model re-fitted: tau up %.0f s, down %.0f s, dead time %.0f s "
                     "(holdout %.2f -> %.2f K)", self.tau_up, self.tau_down, self.dead, prior, fitted)
        return self.reid_last

    # ----------------------------------------------------------- prediction
    def steady(self, output: float, scale: float, base: float) -> float:
        return base + scale * interp(self.lift, output) + self.offset

    def simulate(self, now: float, plan: list[float], spec: OutputSpec, scale: float, base: float,
                 step_s: float = 20.0) -> list[float]:
        """Air every ``step_s`` over the plan (one output per decision period, the first applied
        ``spec.apply_delay_s`` from now), from the filter's state; the dead time from the logged outputs."""
        x = self.x if self.x is not None else base
        horizon = len(plan) * spec.period
        out: list[float] = []
        t = 0.0
        while t < horizon - 1e-6:
            dt = min(step_s, horizon - t)
            at = t + dt / 2 - self.dead
            if at < spec.apply_delay_s:
                output = self.output_at(now + at)
            else:
                output = plan[min(len(plan) - 1, int((at - spec.apply_delay_s) // spec.period))]
            steady = base + scale * interp(self.lift, output) + self.offset
            tau = self.tau_up if steady >= x else self.tau_down
            x += (steady - x) * (1 - math.exp(-dt / tau))
            t += dt
            out.append(x)
        return out

    def output_for(self, air: float, scale: float, base: float) -> float:
        """The output that settles the air at ``air`` (with the current offset)."""
        return inverse(self.lift, max(0.0, (air - base - self.offset) / scale))

    def step_k(self, output: float, scale: float) -> float:
        """Kelvin one level unit around ``output`` gives now (the larger neighbour step)."""
        lower = int(clamp(math.floor(output), 0, LEVEL_MAX - 1))
        up = self.lift[lower + 1] - self.lift[lower]
        down = self.lift[lower] - self.lift[lower - 1] if lower >= 1 else up
        return max(0.2, scale * max(up, down if output == lower else up))

    # ----------------------------------------------------------- persistence
    def to_dict(self) -> dict[str, object]:
        return {"version": self.VERSION, "lift": [round(v, 4) for v in self.lift], "tau_up": round(self.tau_up, 2),
                "tau_down": round(self.tau_down, 2), "dead": round(self.dead, 2), "learned_s": round(self.learned_s),
                "reid_accepted": self.reid_accepted, "reid_tried": self.reid_tried,
                "offset": round(self.offset, 3), "air_in": self.air_in_last, "samples": self.samples,
                "reid_last": self.reid_last}

    @classmethod
    def from_dict(cls, data: object) -> "CoilModel":
        """A model from saved data; anything odd gives the prior (never a broken model)."""
        model = cls()
        if not isinstance(data, dict) or data.get("version") != cls.VERSION:
            return model
        lift = data.get("lift")
        values = [num(v) for v in lift] if isinstance(lift, list) and len(lift) == LEVEL_MAX + 1 else None
        taus = [num(data.get(key)) for key in ("tau_up", "tau_down", "dead")]
        if values is None or any(v is None or not 0 <= v <= LIFT_MAX_K for v in values) or any(v is None for v in taus):
            return model
        if any(b < a - 1e-6 for a, b in zip(values, values[1:])):
            return model
        model.lift = [0.0, *values[1:]]
        model.tau_up = clamp(taus[0], *TAU_RANGE_S)
        model.tau_down = clamp(taus[1], *TAU_RANGE_S)
        model.dead = clamp(taus[2], *DEAD_RANGE_S)
        model.learned_s = max(0.0, num(data.get("learned_s")) or 0.0)
        model.reid_accepted = int(num(data.get("reid_accepted")) or 0)
        model.reid_tried = int(num(data.get("reid_tried")) or 0)
        model.offset = clamp(num(data.get("offset")) or 0.0, -OFFSET_LIMIT_K, OFFSET_LIMIT_K)
        model.samples = int(num(data.get("samples")) or 0)
        last = data.get("reid_last")
        if isinstance(last, dict) and all(num(v) is not None for v in last.values()):
            model.reid_last = {str(k): float(v) for k, v in last.items()}
        air_in = num(data.get("air_in"))
        model.air_in_last = air_in if air_in is not None and -30 < air_in < 50 else None
        return model


# ----------------------------------------------------------------- safety layer
@dataclass
class SafetyFacts:
    """What the actuator knows about the frost lock right now (see SafetyLayer.apply)."""
    # Under the frost lock the air is (or settles) too close to the frost floor: more heat now.
    frost_raise: bool = False
    # Under the frost lock a lower output would settle too close to the frost floor.
    frost_no_lower: bool = False


@dataclass
class SafetyResult:
    output: float
    override: int = OVERRIDE_NONE

    @property
    def text(self) -> str | None:
        return OVERRIDE_TEXT.get(self.override)


@dataclass
class SafetyLayer:
    """The last word on every algorithm's output; no algorithm can go around it.

    In order (a later rule wins): output limits and the move limit, the ping-pong guard,
    the rate limit, then the soft floor (air on its way below target - 0.5 K: more heat,
    never less) and the frost lock (more heat when the air nears the frost floor, never a
    lower output that settles there).
    """
    changes: deque = field(default_factory=deque)
    last_output: float | None = None
    last_raise_at: float | None = None
    hold_output: float | None = None
    hold_until: float | None = None
    overrides: deque = field(default_factory=deque)

    def reset(self) -> None:
        self.changes.clear()
        self.last_output = None
        self.last_raise_at = None
        self.hold_output = self.hold_until = None

    def observe(self, now: float, output: float, period: float, air: float | None = None,
                target: float | None = None) -> None:
        """The actuator's real output (also changes nobody asked for); starts the ping-pong guard."""
        previous = self.last_output
        self.last_output = output
        if previous is None or output == previous:
            return
        direction = 1 if output > previous else -1
        if direction > 0:
            self.last_raise_at = now
        self.changes.append((now, direction))
        while self.changes and now - self.changes[0][0] > RATE_WINDOW_S:
            self.changes.popleft()
        recent = list(self.changes)[-PINGPONG_CHANGES:]
        if len(recent) < PINGPONG_CHANGES or (self.hold_until is not None and now < self.hold_until):
            return
        consecutive = all(b[0] - a[0] <= 1.5 * period for a, b in zip(recent, recent[1:]))
        alternating = all(a[1] == -b[1] for a, b in zip(recent, recent[1:]))
        if consecutive and alternating:
            warm = air is not None and target is not None and air - target > WARM_BIAS_K
            self.hold_output = min(output, previous) if warm else max(output, previous)
            self.hold_until = now + PINGPONG_HOLD_TICKS * period
            LOG.info("Afterheat safety: output went back and forth every tick; %s kept for %d ticks",
                     self.hold_output, PINGPONG_HOLD_TICKS)

    def recent_changes(self, now: float) -> int:
        while self.changes and now - self.changes[0][0] > RATE_WINDOW_S:
            self.changes.popleft()
        return len(self.changes)

    def apply(self, view: ControlView, wanted: float, facts: SafetyFacts | None = None) -> SafetyResult:
        facts = facts or SafetyFacts()
        spec, current, now = view.spec, view.output, view.now
        override = OVERRIDE_NONE
        value = num(wanted)
        if value is None:
            value, override = current, OVERRIDE_FAULT
        bounded = clamp(value, view.low, view.high)
        if bounded != value:
            override = override or OVERRIDE_LIMIT
        out = spec.limit(bounded, current, view.low, view.high)
        if out != spec.snap(bounded) and override == OVERRIDE_NONE:
            override = OVERRIDE_MOVE
        if current < view.low or current > view.high:
            # Outside the allowed range now: straight back in (one move at a time).
            out = spec.limit(clamp(current, view.low, view.high), current, view.low, view.high)
            return self._result(now, SafetyResult(out, OVERRIDE_LIMIT))
        if self.hold_until is not None and now >= self.hold_until:
            self.hold_output = self.hold_until = None
        if self.hold_output is not None and view.low <= self.hold_output <= view.high:
            keep = spec.limit(self.hold_output, current, view.low, view.high)
            if keep != out:
                out, override = keep, OVERRIDE_PINGPONG
        if out != current and self.recent_changes(now) >= RATE_LIMIT_CHANGES and not (
                (out > current and view.air < view.target) or (out < current and view.air > view.target + 1.0)):
            # Too many changes lately: only a change toward the target (cold: up, clearly warm: down).
            out, override = current, OVERRIDE_RATE
        # The soft floor: air below (or within a decision of) target - 0.5 K gets more heat,
        # never less; a raise already on its way is given time to show first.
        floor = view.target - SOFT_FLOOR_K
        model = view.model
        # The model's predictions are only so good: judged with its recent error as a margin.
        margin = clamp(model.prediction_error() or 0.2, 0.1, 0.4)
        cold = view.air < floor or self._heading_below(view, floor + margin)
        # (By the model a raise on its way is in the prediction; by the trend alone it is given time.)
        raise_pending = (model.x is None and self.last_raise_at is not None
                         and now - self.last_raise_at < model.dead + 0.5 * model.tau_up + spec.apply_delay_s)
        if cold and current < view.high:
            if out < current:
                out, override = current, OVERRIDE_FLOOR
            if out == current and not raise_pending:
                out = spec.limit(self._raise(view), current, view.low, view.high)
                override = OVERRIDE_FLOOR if out != current else override
        elif out < current and self._lower_dips(view, out, floor + margin):
            out, override = current, OVERRIDE_FLOOR
        if facts.frost_raise and current < view.high and out <= current:
            out, override = spec.limit(current + max(spec.resolution, 1.0), current, view.low, view.high), OVERRIDE_FROST
        elif facts.frost_no_lower and out < current:
            out, override = current, OVERRIDE_FROST
        return self._result(now, SafetyResult(spec.snap(out), override))

    def _result(self, now: float, result: SafetyResult) -> SafetyResult:
        if result.override != OVERRIDE_NONE:
            self.overrides.append(now)
        while self.overrides and now - self.overrides[0] > 3600:
            self.overrides.popleft()
        return result

    @staticmethod
    def _heading_below(view: ControlView, floor: float) -> bool:
        """Waiting one more decision to raise would let the air fall below ``floor``.

        By the model: the air with the output kept one decision and raised after it (the
        dead time and the lag make the air go on falling for a while after any raise).
        Without a model state, by the measured trend a decision ahead.
        """
        model = view.model
        if model.x is not None:
            scale = model.scale(view.fan, view.water, view.air_in)
            raised = view.spec.limit(SafetyLayer._raise(view), view.output, view.low, view.high)
            if raised <= view.output:
                return False
            path = model.simulate(view.now, [view.output, raised, raised, raised], view.spec, scale,
                                  model.base(view.air_in))
            return min(path) < floor
        if view.trend is not None:
            return view.air + view.trend * view.spec.period < floor
        return False

    @staticmethod
    def _lower_dips(view: ControlView, lower: float, floor: float) -> bool:
        """Lowering to ``lower`` now, even if raised back a decision later, lets the air fall below ``floor``."""
        model = view.model
        if model.x is None:
            return False
        scale = model.scale(view.fan, view.water, view.air_in)
        path = model.simulate(view.now, [lower, view.output, view.output, view.output, view.output], view.spec,
                              scale, model.base(view.air_in))
        return min(path) < floor

    @staticmethod
    def _raise(view: ControlView) -> float:
        """How much more output: one step for HAC1; for a stepless valve enough to settle at the target."""
        spec, current = view.spec, view.output
        if spec.discrete:
            return current + spec.resolution
        scale = view.model.scale(view.fan, view.water, view.air_in)
        need = view.model.output_for(view.target, scale, view.model.base(view.air_in))
        return max(current + 0.5, need)


__all__ = [
    "ALGORITHMS", "ALGORITHM_DEFAULT", "ALGORITHM_CODES", "ALGORITHM_LABELS", "ALGORITHM_HELP",
    "OutputSpec", "HAC1_SPEC", "DIRECT_SPEC", "ControlView", "Decision", "CoilModel", "SafetyLayer",
    "SafetyFacts", "SafetyResult", "OVERRIDE_TEXT", "interp", "inverse",
]
