"""Selectable afterheat algorithms, adapted from Better Thermostat's control modes.

Each algorithm only says which output it wants next (``Decision``); it sees the plant
through ``ControlView`` (with the shared ``CoilModel``) and the allowed outputs through
``OutputSpec`` (whole HAC1 levels at most one a tick, or a stepless 0-100 % valve). The
safety layer and the actuator (afterheat_actuators) do the rest; "standard" is the
existing pilot (afterheat_pilot) and is not here.

Better Thermostat's ``default``, ``fix_calibration`` and ``no_calibration`` modes are not
ported: they correct a radiator valve's own temperature sensor, which the afterheat does
not have ("HAC1 alene" is the native mode).
"""
from __future__ import annotations

import math

from afterheat_control import (ControlView, Decision, LEVEL_MAX, OutputSpec, SOFT_FLOOR_K, clamp, danish, interp,
                               inverse, num)


COLD_COST = 2.0
PEAK_COST = 0.0
PEAK_FROM_K = 3.0


def tracking_cost(error: float, margin: float = 0.0) -> float:
    """Cost per minute (K^2) of air ``error`` K from the target: a dip is much worse than a little warmth.

    Up to +0.3 K warm is nearly free, below the target costs four times more than above it,
    and below the soft floor (target - 0.5 K: a cold draught; ``margin`` K higher for a model
    that predicts only so well) it costs a lot more again.
    """
    if error >= 0:
        return (0.1 * error * error + 0.5 * max(0.0, error - 0.3) ** 2
                + PEAK_COST * max(0.0, error - PEAK_FROM_K) ** 2)
    cost = COLD_COST * error * error
    below = -(error + SOFT_FLOOR_K - margin)
    if below > 0:
        cost += 60.0 * below * below + 15.0 * below
    return cost


class Algorithm:
    """Base: a bumpless start, one decision at a time, and what it learned as plain JSON."""
    name = ""

    def start(self, view: ControlView) -> None:
        """Take over from the output in force now (no bump)."""

    def decide(self, view: ControlView) -> Decision:
        raise NotImplementedError

    def to_dict(self) -> dict[str, object]:
        return {}

    def load(self, data: object) -> None:
        """Restore what ``to_dict`` gave; anything odd is ignored (the defaults stay)."""


def _conditions(view: ControlView) -> tuple[float, float]:
    model = view.model
    return model.scale(view.fan, view.water, view.air_in), model.base(view.air_in)


def _ramp(current: float, goals: list[tuple[int, float]], ticks: int, spec: OutputSpec) -> tuple[float, ...]:
    """Outputs per decision toward ``goals`` [(from tick, output)], moving at most max_move each."""
    plan, value = [], current
    for k in range(ticks):
        goal = goals[0][1]
        for start, output in goals:
            if k >= start:
                goal = output
        value = spec.snap(value + clamp(goal - value, -spec.max_move, spec.max_move))
        plan.append(value)
    return tuple(plan)


def _changes(current: float, plan: tuple[float, ...]) -> list[float]:
    moves, previous = [], current
    for value in plan:
        if value != previous:
            moves.append(value - previous)
        previous = value
    return moves


class Quantizer:
    """Continuous wish -> allowed output; the remainder is carried to the next decision, not rounded away.

    A new output is kept ``hold_s`` unless the wish is at least ``big`` away (Better
    Thermostat's minimum hold with its big-change bypass).
    """

    def __init__(self, hold_s: float, big: float) -> None:
        self.hold_s, self.big = hold_s, big
        self.carry = 0.0
        self.changed_at: float | None = None

    def reset(self) -> None:
        self.carry, self.changed_at = 0.0, None

    def step(self, view: ControlView, wish: float) -> float:
        spec, current = view.spec, view.output
        wanted = wish + self.carry
        out = spec.limit(wanted, current, view.low, view.high)
        held = self.changed_at is not None and view.now - self.changed_at < self.hold_s
        if out != current and held and abs(wanted - current) < self.big:
            out = current
        self.carry = clamp(wanted - out, -spec.resolution, spec.resolution)
        if out != current:
            self.changed_at = view.now
        return out


# ------------------------------------------------------------------- MPC (v2)
class MpcAlgorithm(Algorithm):
    """Better Thermostat MPC v2 adapted to the coil: model + Kalman offset + horizon search.

    The shared CoilModel (lift per output scaled by fan and water, dead time, heating and
    cooling lag, Kalman offset, online and holdout-gated learning) predicts the air for
    every candidate plan over HORIZON_TICKS decisions. Plans are move-blocked (go to one
    output, after k decisions to another, ramping at most max_move a decision). Cost:
    asymmetric tracking (tracking_cost), a penalty per change and a little for the heat
    used. The first output of the best plan is used and the search runs again at the next
    decision (receding horizon).

    HAC1 levels (discrete): a level the MPC stepped down to is kept MIN_DWELL_TICKS before
    it goes up again, a pulse up at least PULSE_TICKS (one: at 26-50 % fan step 2 lifts the
    air 3-5 K a minute, two ticks overshoot), and no plan has a shorter out-and-back visit,
    so the levels do not take turns every tick. Taking turns is planned as a slow duty cycle
    (stay, the neighbour level for some ticks, back for some, and so on: TPI-like, a period
    of 3-12 min) that may also start later: live 2026-11-25 the only way to plan a pulse was
    "now", so the MPC pulsed step 2 with the air still ~1 K over the target. A level HAC1
    took by itself has no dwell (note_sent). The soft floor and the frost lock (safety
    layer) may still raise at once.
    """
    name = "mpc"
    HORIZON_TICKS = 12
    REACH = 3.0
    SWITCH_AFTER = (1, 2, 3, 4, 6, 8)
    # Per change: a HAC1 level step is a felt jump; a stepless valve's small move is not.
    MOVE_COST = 4.0
    # HAC1 levels: the shortest visit, and the duty-cycle plans (ticks on the start level
    # before the first turn; ticks on the other level; ticks back).
    MIN_DWELL_TICKS = 2
    # The shortest visit on the upper level of a turn (a pulse); the lower level keeps MIN_DWELL_TICKS.
    PULSE_TICKS = 1
    CYCLE_START = (0, 1, 2, 3, 4, 6)
    CYCLE_OUT = (2, 3, 4, 6)
    CYCLE_BACK = (2, 3, 4, 6, 8)
    # Staircase plans (see _staircase_plans): ticks before the first step; ticks on the top
    # and on the bottom level (counting the tick it is reached).
    STAIR_START = (0, 1, 2, 3)
    STAIR_TOP = (1, 2, 3)
    STAIR_BOTTOM = (1, 2, 3, 4)
    MOVE_COST_STEPLESS = 0.3
    MOVE_COST_SQUARED = 0.2
    EFFORT_COST = 0.01
    STEP_S = 20.0

    def _candidates(self, view: ControlView, around: float, fine: bool) -> list[float]:
        spec = view.spec
        grid = spec.resolution if spec.discrete else (0.5 if fine else 1.0)
        values, k = [], -int(round(self.REACH / grid))
        while k * grid <= self.REACH + 1e-9:
            value = spec.snap(around + k * grid)
            if view.low <= value <= view.high and value not in values:
                values.append(value)
            k += 1
        return values

    def __init__(self) -> None:
        # The output last sent (after the safety layer) and when: a level HAC1 took by itself
        # instead is not this algorithm's visit and has no minimum dwell.
        self.sent: tuple[float, float] | None = None

    def start(self, view: ControlView) -> None:
        self.sent = None

    def note_sent(self, output: float, now: float) -> None:
        self.sent = (output, now)

    def dwell_left(self, view: ControlView) -> int:
        """Ticks the output in force must still be kept (MIN_DWELL_TICKS from its last change)."""
        outputs = view.model.outputs
        if not view.spec.discrete or len(outputs) < 2 or outputs[-1][1] != view.output:
            return 0
        if self.sent is not None and outputs[-1][1] != self.sent[0] and outputs[-1][0] > self.sent[1]:
            return 0  # HAC1's own step: may be taken back at once
        in_force = (view.now - outputs[-1][0] + view.spec.apply_delay_s) / view.spec.period
        shortest = self.PULSE_TICKS if outputs[-1][1] > outputs[-2][1] else self.MIN_DWELL_TICKS
        return max(0, shortest - int(round(in_force)))

    @staticmethod
    def last_move(view: ControlView) -> int:
        """Direction (+1/-1, 0: none) of the last change of the output in force."""
        outputs = view.model.outputs
        if len(outputs) < 2 or outputs[-1][1] == outputs[-2][1]:
            return 0
        return 1 if outputs[-1][1] > outputs[-2][1] else -1

    def wait_for(self, view: ControlView, direction: int) -> int:
        """Ticks before a move in ``direction``: the dwell only holds back a turn back.

        Going on the same way (a ramp 2 -> 1 -> 0 a step a tick, as HAC1 does by itself) is
        no per-tick turn: live 2026-11-26 the dwell kept a step down from 3 on step 2 for two
        ticks, and the air went on to 26-28.6 C.
        """
        last = self.last_move(view)
        return 0 if last != 0 and direction == last else self.dwell_left(view)

    def _discrete_plans(self, view: ControlView, ticks: int) -> list[tuple[float, ...]]:
        """HAC1 plans: stay; go to a level (ramping) after the dwell; or a duty cycle between two neighbours."""
        spec, current = view.spec, view.output
        plans: list[tuple[float, ...]] = [tuple([current] * ticks)]
        for goal in self._candidates(view, current, fine=True):
            if goal == current:
                continue
            wait = self.wait_for(view, 1 if goal > current else -1)
            for k in sorted({wait + k for k in (0, *self.SWITCH_AFTER) if wait + k < ticks}):
                plans.append(_ramp(current, [(0, current), (k, goal)], ticks, spec))
        step = spec.resolution
        for first in (current - step, current + step):
            if not view.low <= first <= view.high:
                continue
            for second in (first - step, first + step):
                if not view.low <= second <= view.high:
                    continue
                wait = self.wait_for(view, 1 if first > current else -1)
                for k in self.CYCLE_START:
                    k += wait
                    if k >= ticks:
                        continue
                    outs = self.CYCLE_OUT if first < current else sorted({self.PULSE_TICKS, *self.CYCLE_OUT})
                    for out in outs:
                        for back in self.CYCLE_BACK:
                            plan = [current] * k
                            while len(plan) < ticks:
                                plan += [first] * out + [second] * back
                            plans.append(tuple(plan[:ticks]))
        plans.extend(self._staircase_plans(view, ticks))
        return plans

    def _staircase_plans(self, view: ControlView, ticks: int) -> list[tuple[float, ...]]:
        """HAC1's own triangle: a step a tick up to ``top``, kept, a step a tick down to ``bottom``, kept, ...

        Live 2026-11-26 HAC1 alone ran 0 -> 1 -> 2 -> 2 -> 1 -> 0 -> 0 (6-7 min) steadier
        than the MPC's pulses (spread 1.27 against 1.62 K): the ramp spreads a big step's
        heat over two ticks. The MPC picks where in the cycle to start and how long to keep
        the top and the bottom from its model, so the trough sits just above the floor.
        """
        spec, current = view.spec, view.output
        step = spec.resolution
        plans: list[tuple[float, ...]] = []
        for bottom in (current - 2 * step, current - step, current):
            if bottom < view.low:
                continue
            for top in (bottom + 2 * step, bottom + 3 * step):
                if top > view.high or top <= current - step:
                    continue
                for first in (1, -1):  # up first (from the bottom side) or down first (from the top side)
                    goal0 = top if first > 0 else bottom
                    if goal0 == current:
                        continue
                    for k in self.STAIR_START:
                        k += self.wait_for(view, 1 if goal0 > current else -1)
                        if k >= ticks:
                            continue
                        for keep_top in self.STAIR_TOP:
                            for keep_bottom in self.STAIR_BOTTOM:
                                plan, value, goal = [current] * k, current, goal0
                                while len(plan) < ticks:
                                    if value == goal:
                                        plan += [value] * ((keep_top if goal == top else keep_bottom) - 1)
                                        goal = bottom if goal == top else top
                                    value = spec.snap(value + clamp(goal - value, -step, step))
                                    plan.append(value)
                                plans.append(tuple(plan[:ticks]))
        return plans

    def unasked_cost(self, view: ControlView, plan: tuple[float, ...], direction: int) -> float:
        """What it costs (over ``plan``) when HAC1 steps ``direction`` by itself at the coming tick.

        After such a step the level is kept one to three ticks and then the best of a few
        ways on: the plan's own rest, back for good, or back and the next pulse sooner or
        later. For the hold setpoint (afterheat_actuators): where no whole degree
        holds for sure, the step that costs least is the one risked.
        """
        spec, current = view.spec, view.output
        stepped = spec.snap(current + direction * spec.resolution)
        if not view.low <= stepped <= view.high or not plan:
            return 0.0
        scale, base = _conditions(view)
        ticks = len(plan)
        planned = self.cost(view, plan, scale, base)[0]
        upper = spec.snap(current + spec.resolution)
        best = math.inf
        for stay in (1, 2, 3):  # (a step HAC1 took by itself may be taken back at once, see dwell_left)
            prefix = (stepped,) * stay
            ways = [tuple(plan[stay:]), (stepped,) * ticks, (current,) * ticks]
            for wait in (1, 2, 3, 4, 6):
                for on in sorted({self.PULSE_TICKS, self.MIN_DWELL_TICKS}):
                    ways.append(((current,) * wait + (upper,) * on + (current,) * ticks))
            for way in ways:
                candidate = (prefix + way)[:ticks]
                best = min(best, self.cost(view, candidate, scale, base)[0])
        return max(0.0, best - planned)

    def cost(self, view: ControlView, plan: tuple[float, ...], scale: float, base: float) -> tuple[float, float]:
        """(cost, lowest air) of ``plan``."""
        path = view.model.simulate(view.now, list(plan), view.spec, scale, base, self.STEP_S)
        minutes = self.STEP_S / 60.0
        margin = view.model.floor_margin()
        total = sum(tracking_cost(air - view.target, margin) for air in path) * minutes
        per_move = self.MOVE_COST if view.spec.discrete else self.MOVE_COST_STEPLESS
        for move in _changes(view.output, plan):
            total += per_move + self.MOVE_COST_SQUARED * move * move
        total += self.EFFORT_COST * sum(scale * interp(view.model.lift, u) for u in plan) * view.spec.period / 60.0
        return total, min(path) if path else view.air

    def decide(self, view: ControlView) -> Decision:
        scale, base = _conditions(view)
        ticks = self.HORIZON_TICKS
        best: tuple[float, tuple[float, ...], float] | None = None
        seen: set[tuple[float, ...]] = set()

        def consider(plan: tuple[float, ...]) -> None:
            nonlocal best
            if plan in seen:
                return
            seen.add(plan)
            cost, lowest = self.cost(view, plan, scale, base)
            if best is None or cost < best[0] - 1e-9:
                best = (cost, plan, lowest)

        if view.spec.discrete:
            for plan in self._discrete_plans(view, ticks):
                consider(plan)
        else:
            for first in self._candidates(view, view.output, fine=True):
                consider(_ramp(view.output, [(0, first)], ticks, view.spec))
                for k in self.SWITCH_AFTER:
                    for second in self._candidates(view, first, fine=False):
                        if second != first:
                            consider(_ramp(view.output, [(0, first), (k, second)], ticks, view.spec))
        if not view.spec.discrete and best is not None:
            # Stepless: refine the first output in 1 % steps with the rest of the plan kept.
            plan = best[1]
            for delta in (-0.4, -0.3, -0.2, -0.1, 0.1, 0.2, 0.3, 0.4):
                first = view.spec.limit(plan[0] + delta, view.output, view.low, view.high)
                consider((first, *plan[1:]))
        _cost, plan, lowest = best
        path = view.model.simulate(view.now, list(plan), view.spec, scale, base, self.STEP_S)
        per_minute = max(1, int(round(60.0 / self.STEP_S)))
        forecast = tuple(round(air, 2) for air in path[per_minute - 1::per_minute])
        return Decision(plan[0], self._why(view, plan, lowest), plan, forecast)

    def _why(self, view: ControlView, plan: tuple[float, ...], lowest: float) -> str:
        spec, now = view.spec, plan[0]
        if now > view.output:
            doing = f"op til {spec.show(now)} nu"
        elif now < view.output:
            doing = f"ned til {spec.show(now)} nu"
        else:
            doing = f"bliver på {spec.show(now)}"
        later = next(((k, value) for k, value in enumerate(plan) if value != now), None)
        if later is not None:
            minutes = max(1, round(later[0] * spec.period / 60))
            doing += f", {spec.show(later[1])} om ca. {minutes} min"
        horizon = round(self.HORIZON_TICKS * spec.period / 60)
        return f"Ser {horizon} min frem: {doing} · laveste luft ca. {danish(lowest)} °C"


# ------------------------------------------------------------ MPC simple (v1)
class SimpleMpcAlgorithm(Algorithm):
    """Better Thermostat MPC v1 adapted: one-state model, EMA-adapted gain and loss, coarse grid search.

        air(k+1) = air(k) + gain * output(shown) * dt/tau - (air(k) - air_in) * dt/tau

    i.e. a straight line from output to lift (gain K per level unit around where it runs),
    adapted online by an exponential average of the prediction error (gain while heating,
    the lag while the valve is shut). Every decision one constant output is tried over
    HORIZON_TICKS (coarse grid, then 1 % for a stepless valve); cost: undershoot nine times
    overshoot (Better Thermostat's overshoot penalty, turned round), a control penalty
    toward the steady output and a change penalty; a new output is kept MIN_HOLD_S.
    """
    name = "mpc_simple"
    HORIZON_TICKS = 6
    UNDER_WEIGHT = 9.0
    CONTROL_PENALTY = 0.05
    CHANGE_PENALTY = 1.0
    ADAPT_ALPHA = 0.1
    GAIN_RANGE = (0.3, 20.0)
    TAU_RANGE = (60.0, 600.0)
    MIN_HOLD_TICKS = 2

    def __init__(self) -> None:
        self.gain: float | None = None
        self.tau = 170.0
        self.previous: tuple[float, float] | None = None
        self.changed_at: float | None = None

    def start(self, view: ControlView) -> None:
        if self.gain is None:
            scale, _base = _conditions(view)
            self.gain = clamp(view.model.step_k(max(view.output, 1.0), scale), *self.GAIN_RANGE)
        self.previous, self.changed_at = None, None

    def _adapt(self, view: ControlView, base: float) -> None:
        if self.previous is not None:
            at, air = self.previous
            dt = view.now - at
            if 20.0 <= dt <= 300.0:
                shown = view.model.output_at(view.now - view.model.dead - dt / 2)
                rate = dt / self.tau
                predicted = (self.gain * shown - (air - base)) * rate
                error = (view.air - air) - predicted
                if shown > 0.05:
                    self.gain = clamp(self.gain + self.ADAPT_ALPHA * error / (shown * rate), *self.GAIN_RANGE)
                elif abs(air - base) > 0.5:
                    observed = -(view.air - air) / ((air - base) * dt)
                    if observed > 0:
                        self.tau = clamp(self.tau + self.ADAPT_ALPHA * (1.0 / observed - self.tau), *self.TAU_RANGE)
        self.previous = (view.now, view.air)

    def _cost(self, view: ControlView, output: float, base: float) -> float:
        spec = view.spec
        air, total = view.air, 0.0
        u_ss = clamp((view.target - base) / self.gain, view.low, view.high)
        shown_now = view.model.output_at(view.now - view.model.dead)
        delay_ticks = max(1, round((view.model.dead + spec.apply_delay_s) / spec.period))
        value = view.output
        for k in range(self.HORIZON_TICKS):
            value = spec.snap(value + clamp(output - value, -spec.max_move, spec.max_move))
            drive = base + self.gain * (shown_now if k < delay_ticks else value)
            air += (drive - air) * (1 - math.exp(-spec.period / self.tau))
            error = air - view.target
            total += error * error * (self.UNDER_WEIGHT if error < 0 else 1.0)
        return total + self.CONTROL_PENALTY * (output - u_ss) ** 2 + self.CHANGE_PENALTY * (output - view.output) ** 2

    def decide(self, view: ControlView) -> Decision:
        _scale, base = _conditions(view)
        if self.gain is None:
            self.start(view)
        self._adapt(view, base)
        spec = view.spec
        coarse = spec.resolution if spec.discrete else 1.0
        options = sorted({spec.limit(view.output + k * coarse, view.output, view.low, view.high)
                          for k in range(-int(spec.max_move / coarse), int(spec.max_move / coarse) + 1)} | {view.output})
        best = min(options, key=lambda u: self._cost(view, u, base))
        if not spec.discrete:
            fine = {spec.limit(best + k * spec.resolution, view.output, view.low, view.high) for k in range(-9, 10)}
            best = min(fine, key=lambda u: self._cost(view, u, base))
        held = self.changed_at is not None and view.now - self.changed_at < self.MIN_HOLD_TICKS * spec.period - 20
        if best != view.output and held and abs(view.air - view.target) < 1.0:
            return Decision(view.output, f"Holder {spec.show(view.output)} lidt endnu (mindste tid pr. skift)")
        if best != view.output:
            self.changed_at = view.now
        settle = base + self.gain * best
        return Decision(best, f"Ser {round(self.HORIZON_TICKS * spec.period / 60)} min frem med én simpel model: "
                              f"{spec.show(best)} giver på sigt ca. {danish(settle)} °C")

    def to_dict(self) -> dict[str, object]:
        return {"gain": None if self.gain is None else round(self.gain, 4), "tau": round(self.tau, 1)}

    def load(self, data: object) -> None:
        if not isinstance(data, dict):
            return
        gain, tau = num(data.get("gain")), num(data.get("tau"))
        if gain is not None:
            self.gain = clamp(gain, *self.GAIN_RANGE)
        if tau is not None:
            self.tau = clamp(tau, *self.TAU_RANGE)


# ------------------------------------------------------------------------ PID
class PidAlgorithm(Algorithm):
    """Better Thermostat's PID, retuned to the coil's minute: P + I + D on the measurement.

    Gains follow the SIMC rule for a lag with dead time (Kp = tau / (K (tau_c + dead)),
    Ti = tau, Td small), with K the kelvin one level gives here now (from the shared model):
    a big step (level 1 -> 2) gets a gentler gain than a small one. Anti-windup by
    conditional integration, 20 % integrator relief when the error changes sign close to
    the target, and Better Thermostat's auto-tune (per 0.5 K target bucket, every 5 min:
    too warm on average -> less P and I, too cold and slow -> more). The continuous output
    is quantized with the remainder carried to the next decision (Quantizer), kept at least
    two decisions unless two whole levels off.
    """
    name = "pid"
    TAU_C_S = 90.0
    TD_S = 15.0
    D_SMOOTHING = 0.5
    RELIEF = 0.8
    RELIEF_BAND_K = 0.1
    TUNE_EVERY_S = 300.0
    TUNE_RANGE = (0.3, 3.0)
    MAX_DT_S = 600.0

    def __init__(self) -> None:
        self.integral: float | None = None
        self.last: tuple[float, float, float] | None = None  # (now, smoothed air, error)
        self.tune: dict[str, list[float]] = {}
        self.window: list[float] = []
        self.tuned_at: float | None = None
        self.quantizer = Quantizer(hold_s=2 * 60.0 - 20, big=2.0)
        self.parts = (0.0, 0.0, 0.0)

    def _bucket(self, target: float) -> str:
        return f"{math.floor(target * 2) / 2:.1f}"

    def _gains(self, view: ControlView) -> tuple[float, float, float]:
        scale, _base = _conditions(view)
        model = view.model
        k = model.step_k(view.output, scale)
        dead = model.dead + view.spec.apply_delay_s
        tau = model.tau_up
        kp = tau / (k * (self.TAU_C_S + dead))
        ki = kp / max(tau, 30.0)
        kd = kp * self.TD_S
        mp, mi, md = self.tune.get(self._bucket(view.target), [1.0, 1.0, 1.0])
        return kp * mp, ki * mi, kd * md

    def start(self, view: ControlView) -> None:
        kp, _ki, _kd = self._gains(view)
        self.integral = view.output - kp * (view.target - view.air)  # bumpless
        self.last = None
        self.quantizer.reset()
        self.window, self.tuned_at = [], view.now

    def _autotune(self, view: ControlView, error: float) -> None:
        self.window.append(error)
        if self.tuned_at is None:
            self.tuned_at = view.now
        if view.now - self.tuned_at < self.TUNE_EVERY_S or len(self.window) < 3:
            return
        mean = sum(self.window) / len(self.window)
        self.window, self.tuned_at = [], view.now
        mp, mi, md = self.tune.get(self._bucket(view.target), [1.0, 1.0, 1.0])
        if mean < -0.5:      # too warm on average (error = target - air): less push
            mp, mi, md = mp * 0.9, mi * 0.8, md * 1.1
        elif mean > 0.3:     # too cold on average: more
            mp, mi = mp * 1.1, mi * 1.2
        elif abs(mean) < 0.1:
            mi *= 0.95
        low, high = self.TUNE_RANGE
        self.tune[self._bucket(view.target)] = [clamp(mp, low, high), clamp(mi, low, high), clamp(md, low, high)]

    def decide(self, view: ControlView) -> Decision:
        if self.integral is None:
            self.start(view)
        kp, ki, kd = self._gains(view)
        error = view.target - view.air
        if self.last is None:
            smoothed, dt, derivative = view.air, 0.0, 0.0
        else:
            at, previous, last_error = self.last
            dt = clamp(view.now - at, 0.0, self.MAX_DT_S)
            smoothed = previous + self.D_SMOOTHING * (view.air - previous)
            derivative = (smoothed - previous) / dt if dt > 0 else 0.0
            if last_error * error < 0 and abs(error) <= self.RELIEF_BAND_K:
                self.integral = view.output + (self.integral - view.output) * self.RELIEF
        p, d = kp * error, -kd * derivative
        unsaturated = p + self.integral + d
        pushing_out = (unsaturated >= view.high and error > 0) or (unsaturated <= view.low and error < 0)
        if dt > 0 and not pushing_out:
            self.integral = clamp(self.integral + ki * error * dt, view.low - 1.0, view.high + 1.0)
        wish = clamp(p + self.integral + d, view.low - 0.5, view.high + 0.5)
        self.last = (view.now, smoothed, error)
        self._autotune(view, error)
        self.parts = (p, self.integral, d)
        out = self.quantizer.step(view, wish)
        return Decision(out, f"Ønsker {danish(wish, 2) if view.spec.discrete else view.spec.show(wish)} "
                             f"(nu {danish(p, 2)} + samlet {danish(self.integral, 2)} + ændring {danish(d, 2)}) "
                             f"→ {view.spec.show(out)}")

    def to_dict(self) -> dict[str, object]:
        return {"tune": {key: [round(v, 4) for v in values] for key, values in self.tune.items()}}

    def load(self, data: object) -> None:
        tune = data.get("tune") if isinstance(data, dict) else None
        if not isinstance(tune, dict):
            return
        low, high = self.TUNE_RANGE
        for key, values in tune.items():
            if (isinstance(key, str) and isinstance(values, list) and len(values) == 3
                    and all(num(v) is not None for v in values)):
                self.tune[key] = [clamp(float(v), low, high) for v in values]


# ------------------------------------------------------------------------ TPI
class TpiAlgorithm(Algorithm):
    """Better Thermostat's TPI: duty = coef_int * (target - air) + the outside term.

    Better Thermostat's outside term (coef_ext * (target - outdoor)) becomes the lift the
    air needs from the coil now (target - air before the coil, through the shared model's
    lift per output, fan and water). With HAC1's whole levels the duty is time-shared
    between the two levels around it in fixed rounds of CYCLE_TICKS decisions (the upper
    level first); with a stepless valve the duty is the opening itself (classic TPI). Like
    Better Thermostat, warmer than the target by THRESHOLD_HIGH_K at the start of a round
    gives that round no upper level. No learning of its own and no integral.
    """
    name = "tpi"
    COEF_INT = 0.6
    CYCLE_TICKS = 6
    THRESHOLD_HIGH_K = 0.3

    def __init__(self) -> None:
        self.cycle_at: float | None = None
        self.pair: tuple[float, int] | None = None
        self.duty = 0.0

    def start(self, view: ControlView) -> None:
        self.cycle_at, self.pair = None, None

    def _demand(self, view: ControlView) -> float:
        scale, base = _conditions(view)
        lift = view.target - base + self.COEF_INT * (view.target - view.air)
        return inverse(view.model.lift, max(0.0, lift - view.model.offset) / scale)

    def decide(self, view: ControlView) -> Decision:
        spec = view.spec
        demand = clamp(self._demand(view), view.low, view.high)
        if not spec.discrete:
            self.duty = demand / LEVEL_MAX
            out = spec.limit(demand, view.output, view.low, view.high)
            return Decision(out, f"Åbner {spec.show(demand)}: varmen luften mangler plus {danish(self.COEF_INT)} × afvigelsen")
        cycle_s = self.CYCLE_TICKS * spec.period
        if self.cycle_at is None or view.now - self.cycle_at >= cycle_s - 5:
            self.cycle_at = view.now
            lower = int(clamp(math.floor(demand), view.low, max(view.low, view.high - 1)))
            fraction = clamp(demand - lower, 0.0, 1.0)
            if view.air - view.target > self.THRESHOLD_HIGH_K:
                fraction = 0.0
            self.duty = fraction
            self.pair = (float(lower), int(round(fraction * self.CYCLE_TICKS)))
        lower, on_ticks = self.pair
        tick = int((view.now - self.cycle_at + 5) // spec.period)
        want = lower + 1 if tick < on_ticks and lower + 1 <= view.high else lower
        out = spec.limit(want, view.output, view.low, view.high)
        return Decision(out, f"Runde på {round(cycle_s / 60)} min: trin {int(lower) + 1} i {on_ticks} min og "
                             f"trin {int(lower)} resten (minut {tick + 1})")


# ----------------------------------------------------------------- Heat power
class HeatPowerAlgorithm(Algorithm):
    """Better Thermostat's heating-power mode for the coil: feed-forward from the learned lift.

    The output that should give the target is read off the shared model's learned lift per
    output at today's fan, water and air before the coil (the coil's "heating power"),
    plus a slow trim (integral, TRIM_TIME_S per kelvin and level, anti-windup) for what the
    lift does not know. Quantized with the remainder carried, a level kept at least three
    decisions unless two whole levels off.
    """
    name = "heat_power"
    TRIM_TIME_S = 900.0
    TRIM_LIMIT = 2.0

    def __init__(self) -> None:
        self.trim: float | None = None
        self.last: float | None = None
        self.quantizer = Quantizer(hold_s=3 * 60.0 - 20, big=2.0)
        self.feed = 0.0

    def _feed(self, view: ControlView) -> float:
        scale, base = _conditions(view)
        return inverse(view.model.lift, max(0.0, view.target - base) / scale)

    def start(self, view: ControlView) -> None:
        self.trim = clamp(view.output - self._feed(view), -self.TRIM_LIMIT, self.TRIM_LIMIT)
        self.last = None
        self.quantizer.reset()

    def decide(self, view: ControlView) -> Decision:
        if self.trim is None:
            self.start(view)
        feed = self.feed = self._feed(view)
        scale, _base = _conditions(view)
        error = view.target - view.air
        dt = 0.0 if self.last is None else clamp(view.now - self.last, 0.0, 600.0)
        self.last = view.now
        wish = feed + self.trim
        pushing_out = (wish >= view.high and error > 0) or (wish <= view.low and error < 0)
        if dt > 0 and not pushing_out:
            k = view.model.step_k(view.output, scale)
            self.trim = clamp(self.trim + error * dt / (self.TRIM_TIME_S * k), -self.TRIM_LIMIT, self.TRIM_LIMIT)
        wish = clamp(feed + self.trim, view.low - 0.5, view.high + 0.5)
        out = self.quantizer.step(view, wish)
        return Decision(out, f"Varmen pr. trin siger {danish(feed, 2)}, rettet {danish(self.trim, 2)} "
                             f"→ {view.spec.show(out)}")

    def to_dict(self) -> dict[str, object]:
        return {"trim": None if self.trim is None else round(self.trim, 4)}

    def load(self, data: object) -> None:
        trim = num(data.get("trim")) if isinstance(data, dict) else None
        if trim is not None:
            self.trim = clamp(trim, -self.TRIM_LIMIT, self.TRIM_LIMIT)


ALGORITHM_CLASSES: dict[str, type[Algorithm]] = {
    cls.name: cls for cls in (MpcAlgorithm, SimpleMpcAlgorithm, PidAlgorithm, TpiAlgorithm, HeatPowerAlgorithm)}


def make_algorithm(name: str) -> Algorithm | None:
    """A fresh algorithm by its setting name; None for "standard" (the pilot) or unknown names."""
    cls = ALGORITHM_CLASSES.get(name)
    return cls() if cls is not None else None


__all__ = ["Algorithm", "MpcAlgorithm", "SimpleMpcAlgorithm", "PidAlgorithm", "TpiAlgorithm", "HeatPowerAlgorithm",
           "ALGORITHM_CLASSES", "make_algorithm", "tracking_cost", "Quantizer"]
