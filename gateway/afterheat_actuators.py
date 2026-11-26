"""Actuators for the selectable afterheat algorithms, and the glue that runs them.

* ``AlgorithmControl``: the shared coil model, one instance of each algorithm (so a switch
  back keeps what it learned), the safety layer, a fault fallback and the store file.
* ``AlgorithmPilot``: today's actuator, HAC1 through its whole-degree setpoint, one level a
  tick. It is the standard pilot (afterheat_pilot.AfterheatPilot) with one seam: with any
  other algorithm than "standard" the level the pilot plans is the algorithm's choice after
  the safety layer; tick timing, setpoint choice, HAC1 follow check and native fallback
  stay the pilot's. With "standard" every call goes straight to the pilot, unchanged.
* ``DirectValveController``: a stepless 0-10 V valve driven by the Pi directly. It does
  not exist in hardware yet; the tests and the simulator use it.
"""
from __future__ import annotations

import json
import logging
import math
import os
from collections import deque
from dataclasses import replace
from pathlib import Path

from afterheat_algorithms import Algorithm, make_algorithm
from afterheat_data import MpcWatch, Telemetry, build_block, predict
from afterheat_control import (ALGORITHM_CODES, ALGORITHM_DEFAULT, ALGORITHM_LABELS, ALGORITHMS, DIRECT_SPEC,
                               FROST_MARGIN_K, HAC1_SPEC, OVERRIDE_FAULT, OVERRIDE_NONE, CoilModel, ControlView,
                               Decision, OutputSpec, SafetyFacts, SafetyLayer, SafetyResult, clamp, danish, num)
from afterheat_pilot import (HAC1_DOWN_BAND_K, LEVEL_MAX, SETPOINT_MAX, SETPOINT_MIN,
                             STEP_COST_FORBIDDEN, TICK_PERIOD_S, AfterheatPilot, PilotInputs)

LOG = logging.getLogger("afterheat-control")

STORE_VERSION = 1
SAVE_EVERY_S = 10 * 60.0
# The same decision is reused this long when the actuator asks again (e.g. HAC1's clock unknown).
REUSE_DECISION_S = 20.0
FAULT_LIMIT = 3
FAULT_PAUSE_S = 30 * 60.0
# The MPC's hold setpoint (AlgorithmPilot._model_hold): the spread (K) of the air HAC1 reads
# at its tick around the model's guess, and the least a step HAC1 takes by itself costs.
HOLD_SPREAD_K = 0.12
HOLD_SPREAD_SHARE = 0.15
# HAC1's tick is known to a few seconds while the levels change often (TickClock).
HOLD_PHASE_S = 4.0
UNASKED_MIN_COST = 0.5
# The model's air at the tick is checked against the air read just after it, per phase
# (rising, near a peak or trough, falling): the mean miss is learned and taken off (the model's
# lag and dead time are fitted on minutes, the peaks after a pulse were ~0.35 K lower).
HOLD_BIAS_SPLIT_K = 0.3
HOLD_BIAS_WEIGHT = 0.2
HOLD_BIAS_LIMIT_K = 1.0
HOLD_CHECK_AFTER_S = 3.0


class AlgorithmControl:
    """Model + algorithms + safety for one actuator; persists what was learned."""

    def __init__(self, spec: OutputSpec, store_path: str | Path | None = None) -> None:
        self.spec = spec
        self.store_path = Path(store_path) if store_path else None
        self.model = CoilModel()
        self.algorithms: dict[str, Algorithm] = {}
        self.saved: dict[str, object] = {}
        self.safety = SafetyLayer()
        self.telemetry = Telemetry()
        self.name = ALGORITHM_DEFAULT
        self.started = False
        self.decision: Decision | None = None
        self.decided_at: float | None = None
        self.decided_output: float | None = None
        self.result: SafetyResult | None = None
        self.faults = 0
        self.paused_until: float | None = None
        self.saved_at: float | None = None
        self.load()

    # ------------------------------------------------------------- algorithms
    def algorithm(self, name: str | None = None) -> Algorithm | None:
        name = self.name if name is None else name
        if name not in self.algorithms:
            algorithm = make_algorithm(name)
            if algorithm is None:
                return None
            algorithm.load(self.saved.get(name))
            self.algorithms[name] = algorithm
        return self.algorithms[name]

    def select(self, name: str, now: float) -> bool:
        """Switch to ``name`` (unknown -> standard); True when it changed."""
        name = name if name in ALGORITHMS else ALGORITHM_DEFAULT
        if name == self.name:
            return False
        LOG.info("Afterheat algorithm: %s -> %s", self.name, name)
        self.name = name
        self.restart()
        self.save(now, force=True)
        return True

    def restart(self) -> None:
        """Next decision starts the algorithm afresh from the output in force (bumpless)."""
        self.started = False
        self.decision = self.decided_at = self.decided_output = None
        self.result = None
        self.safety.reset()

    @property
    def paused(self) -> bool:
        return self.paused_until is not None

    def observe(self, now: float, air: float | None, output: float | None, fan: float | None,
                water: float | None, air_in: float | None, target: float | None) -> None:
        if num(air) is None or num(output) is None:
            return
        self.model.observe(now, float(air), float(output), fan, water, air_in)
        self.safety.observe(now, float(output), self.spec.period, air, target)

    def decide(self, view: ControlView, facts: SafetyFacts) -> tuple[Decision, SafetyResult] | None:
        """The algorithm's choice through the safety layer; None: fall back (fault or paused)."""
        if self.paused_until is not None:
            if view.now < self.paused_until:
                return None
            self.paused_until, self.faults = None, 0
            self.started = False
        algorithm = self.algorithm()
        if algorithm is None:
            return None
        reuse = (self.decision is not None and self.decided_at is not None
                 and 0 <= view.now - self.decided_at < REUSE_DECISION_S and self.decided_output == view.output)
        if not reuse:
            try:
                if not self.started:
                    algorithm.start(view)
                    self.started = True
                decision = algorithm.decide(view)
                if num(decision.output) is None:
                    raise ValueError(f"non-finite output {decision.output!r}")
            except Exception:  # noqa: BLE001 - an algorithm's bug never leaves the coil unsteered
                self.faults += 1
                LOG.exception("Afterheat algorithm %s failed (%d in a row)", self.name, self.faults)
                if self.faults >= FAULT_LIMIT:
                    self.paused_until = view.now + FAULT_PAUSE_S
                    LOG.warning("Afterheat algorithm %s paused %d min; the standard pilot steers",
                                self.name, FAULT_PAUSE_S // 60)
                self.result = SafetyResult(view.output, OVERRIDE_FAULT)
                return None
            self.faults = 0
            self.decision, self.decided_at, self.decided_output = decision, view.now, view.output
        self.result = self.safety.apply(view, self.decision.output, facts)
        note = getattr(algorithm, "note_sent", None)
        if note is not None:
            note(self.result.output, view.now)
        return self.decision, self.result

    # ------------------------------------------------------------ persistence
    def to_dict(self) -> dict[str, object]:
        algorithms = dict(self.saved)
        algorithms.update({name: algorithm.to_dict() for name, algorithm in self.algorithms.items()})
        return {"version": STORE_VERSION, "model": self.model.to_dict(), "algorithms": algorithms,
                "energy": self.telemetry.energy_dict()}

    def load(self) -> None:
        if self.store_path is None:
            return
        try:
            data = json.loads(self.store_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as error:
            LOG.warning("Afterheat algorithm store not read (%s); starting from the prior", error)
            return
        self.restore(data)

    def restore(self, data: object) -> None:
        if not isinstance(data, dict) or data.get("version") != STORE_VERSION:
            return
        self.model = CoilModel.from_dict(data.get("model"))
        self.telemetry.load_energy(data.get("energy"))
        algorithms = data.get("algorithms")
        self.saved = {k: v for k, v in algorithms.items() if isinstance(k, str)} if isinstance(algorithms, dict) else {}
        self.algorithms = {}

    def save(self, now: float, force: bool = False) -> None:
        if self.store_path is None:
            return
        if not force and self.saved_at is not None and now - self.saved_at < SAVE_EVERY_S:
            return
        self.saved_at = now
        try:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.store_path.with_name(self.store_path.name + ".tmp")
            tmp.write_text(json.dumps(self.to_dict(), separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, self.store_path)
        except OSError as error:
            LOG.warning("Afterheat algorithm store not saved: %s", error)

    def status_summary(self) -> dict[str, object]:
        """The model's state word: learning (under 30 min or no score yet), uncertain, ready."""
        error = self.model.prediction_error()
        if self.model.learned_s / 60 < 30 or error is None:
            return {"state": "learning"}
        return {"state": "uncertain" if error > 0.5 else "ready"}

    def status(self) -> dict[str, object]:
        """What the shared model has learned, in plain terms for the WebUI (see learning_state)."""
        model = self.model
        error = model.prediction_error()
        learned_minutes = model.learned_s / 60
        state = self.status_summary()["state"]
        scale = model.last_scale
        return {
            "state": state,
            # Kelvin each level adds now (the fan and water of the last look), levels 1..4.
            "step_k_now": [round(scale * (model.lift[n] - model.lift[n - 1]), 2) for n in range(1, 5)],
            "lift_k": [round(v, 2) for v in model.lift],
            "tau_up_s": round(model.tau_up), "tau_down_s": round(model.tau_down), "dead_s": round(model.dead),
            "offset_k": round(model.offset, 2), "learned_minutes": round(learned_minutes),
            "samples": model.samples, "buffer_minutes": round(len(model.buffer) * 10 / 60),
            "prediction_error_k": None if error is None else round(error, 2),
            "prediction_checks": len(model.check_errors),
            "refits": model.reid_accepted, "refit_attempts": model.reid_tried,
            "refit_last": model.reid_last,
            "safety_overrides_hour": len(self.safety.overrides),
            "algorithms": {name: algorithm.to_dict() for name, algorithm in self.algorithms.items()},
        }


class AlgorithmPilot(AfterheatPilot):
    """The standard pilot as HAC1's actuator for any selectable algorithm (see module doc)."""

    def __init__(self, period: float = TICK_PERIOD_S, store_path: str | Path | None = None) -> None:
        self.control = AlgorithmControl(replace(HAC1_SPEC, period=period), store_path)
        self.algo_wish: float | None = None
        self.prediction: dict[str, object] = {}
        self.hold_bias = {"rise": 0.0, "flat": 0.0, "fall": 0.0}
        self.hold_checks: deque[tuple[float, float, str]] = deque()
        self.mpc_watch = MpcWatch()
        super().__init__(period)

    @property
    def algorithm_name(self) -> str:
        return self.control.name

    def _steering(self) -> bool:
        """An algorithm (not the standard pilot) chooses the level right now."""
        return self.control.name != ALGORITHM_DEFAULT and not self.control.paused

    def update(self, inp: PilotInputs) -> dict[str, object]:
        if self.control.select(getattr(inp, "algorithm", ALGORITHM_DEFAULT) or ALGORITHM_DEFAULT, inp.now):
            if self.active:
                self._deactivate()  # the next update starts the pilot and the algorithm bumplessly
        self._feed_model(inp)
        result = super().update(inp)
        self._check_holds(inp.now)
        if self.active and self._steering() and self.control.name == "mpc" and inp.afterheat_on:
            self.mpc_watch.sample(inp.now, self._raw(inp) if self._fresh(inp.t2ah, inp.t2ah_age) else None,
                                  num(inp.target))
        else:
            self.mpc_watch.reset()
        self.control.save(inp.now)
        return result

    def _check_holds(self, now: float) -> None:
        """Learn how far the model's air at the tick missed (see HOLD_BIAS_WEIGHT)."""
        while self.hold_checks and now >= self.hold_checks[0][0] + HOLD_CHECK_AFTER_S:
            at, predicted, phase = self.hold_checks.popleft()
            seen = [v for t, v in self.raw_history if abs(t - at - HOLD_CHECK_AFTER_S) <= HOLD_CHECK_AFTER_S]
            if seen and now - at <= 4 * HOLD_CHECK_AFTER_S:
                miss = clamp(seen[0] - predicted, -2 * HOLD_BIAS_LIMIT_K, 2 * HOLD_BIAS_LIMIT_K)
                bias = self.hold_bias[phase] + HOLD_BIAS_WEIGHT * (miss - self.hold_bias[phase])
                self.hold_bias[phase] = clamp(bias, -HOLD_BIAS_LIMIT_K, HOLD_BIAS_LIMIT_K)

    def _feed_model(self, inp: PilotInputs) -> None:
        """Every fresh look at the coil teaches the shared model, whoever steers (also HAC1 alone)."""
        if not (inp.afterheat_on and self._fresh(inp.level, inp.level_age) and self._fresh(inp.t2ah, inp.t2ah_age)):
            return
        target = num(inp.target) if inp.target is not None else None
        self.control.observe(inp.now, self._raw(inp), float(int(inp.level)), inp.fan_supply, inp.water_flow,
                             inp.before_heater, target)
        self.control.telemetry.sample(inp.now, self._raw(inp), target, float(int(inp.level)))
        # The model's view a minute and ten minutes ahead, once a tick (read-only, for the HA block).
        self.prediction = predict(self.control.model, inp.now)

    def _view(self, inp: PilotInputs, low: int, high: int) -> ControlView:
        return ControlView(
            now=inp.now, target=float(inp.target), air=self._raw(inp), output=float(int(inp.level)),
            low=float(low), high=float(high), spec=self.control.spec, model=self.control.model,
            fan=_fan(inp.fan_supply), water=num(inp.water_flow), air_in=num(inp.before_heater),
            frost_lock=inp.frost_lock, frost_floor=float(inp.frost_floor), trend=self._trend(inp.now))

    def _plan(self, inp: PilotInputs, wanted: float, low: int, high: int) -> tuple[str, int]:
        self.step_cleared = False
        if self.control.name == ALGORITHM_DEFAULT:
            return super()._plan(inp, wanted, low, high)
        level = int(inp.level)
        self.gate, self.gate_dir, self.gate_step_k = None, 0, 0.0
        facts = SafetyFacts(
            frost_raise=inp.frost_lock and level < high and (
                self.frost_cold or self._below_frost_floor(inp, self.error_settled)
                or self._fresh_frost_trap(inp, level)),
            frost_no_lower=self._down_below_frost_floor(inp, level))
        self.frost_up = facts.frost_raise
        chosen = self.control.decide(self._view(inp, low, high), facts)
        if chosen is None:
            self.algo_wish = None
            return super()._plan(inp, wanted, low, high)  # fault or paused: the standard pilot's plan
        decision, safety = chosen
        target = int(round(safety.output))
        self.step_cleared = target != level
        self.algo_wish = decision.output
        if target != level or safety.override != OVERRIDE_NONE:
            LOG.info("Afterheat[%s]: level %d, wants %s -> %d%s", self.control.name, level,
                     _short(decision.output), target, f" (safety: {safety.text})" if safety.text else "")
        if target > level:
            return "up", target
        if target < level:
            return "down", target
        return "hold", level

    def _choose_setpoint(self, inp: PilotInputs, action: str, wanted: float, low: int,
                         ahead: float = 0.0) -> tuple[str, int, str | None, float, str]:
        if (action == "hold" and ahead > 0 and self._steering() and self.control.name == "mpc"
                and self.control.decision is not None):
            chosen = self._model_hold(inp, low, ahead)
            if chosen is not None:
                return chosen
        return super()._choose_setpoint(inp, action, wanted, low, ahead)

    def _model_hold(self, inp: PilotInputs, low: int, ahead: float) -> tuple[str, int, None, float, str] | None:
        """The hold setpoint from the shared model's air at HAC1's tick and the algorithm's plan.

        HAC1 holds for T2AH in [sp, sp + 0.95) at its tick. Taking turns between levels ~8 K
        apart the air sweeps 1-5 K a minute, and the pilot's guess (the trend of the last
        30 s) misses the turn at the peak or the trough: live 2026-11-25 HAC1 stepped by
        itself on most ticks. The model knows the levels on their way (dead time, lag), so
        its air at the tick is the guess. Where no setpoint is safe, the step HAC1 may take
        is weighed by what it costs by the MPC's own measure (MpcAlgorithm.unasked_cost: a
        pulse cut short is cheap, a step on to 3 or down to 0 is not), plus a move when it
        would end a level still in its minimum dwell. The guess's usual miss is learned per
        phase (_check_holds).
        """
        level = int(inp.level)
        model = self.control.model
        if model.x is None or level <= 0 or level >= LEVEL_MAX:
            return None  # (the pilot's own rules for the ends: a hold there is asked for as a step)
        scale = model.scale(_fan(inp.fan_supply), num(inp.water_flow), num(inp.before_heater))
        base = model.base(num(inp.before_heater))
        spec = OutputSpec(period=ahead, apply_delay_s=ahead)  # only the outputs already given count
        path = model.simulate(inp.now, [float(level)], spec, scale, base, step_s=5.0)
        change = path[-1] - model.x if path else 0.0
        phase = "rise" if change > HOLD_BIAS_SPLIT_K else ("fall" if change < -HOLD_BIAS_SPLIT_K else "flat")
        guess = self._raw(inp) + change
        self.hold_checks.append((inp.now + ahead, guess, phase))
        air = guess + self.hold_bias[phase]
        spread = HOLD_SPREAD_K + HOLD_SPREAD_SHARE * abs(change) + abs(change) / ahead * HOLD_PHASE_S
        algorithm = self.control.algorithm()
        view = self._view(inp, low, LEVEL_MAX)
        plan = tuple(self.control.decision.plan) or (float(level),) * algorithm.HORIZON_TICKS
        # A level still in its minimum dwell left after one tick is the per-tick turn that must not be.
        kept = algorithm.dwell_left(view) > 0

        def harm(direction: int) -> float:
            if direction < 0 and self._step_cost(inp, low, -1) >= STEP_COST_FORBIDDEN:
                return STEP_COST_FORBIDDEN
            extra = algorithm.unasked_cost(view, plan, direction)
            return max(UNASKED_MIN_COST, extra) + (algorithm.MOVE_COST if kept else 0.0)
        harm_up, harm_down = harm(1), harm(-1)
        floor_sp = max(SETPOINT_MIN, math.ceil(inp.frost_floor) if inp.frost_lock else SETPOINT_MIN)

        def likely(z: float) -> float:
            return 0.5 * (1 + math.erf(z / math.sqrt(2)))

        def risk(candidate: int) -> float:
            return (likely((candidate - air) / spread) * harm_up
                    + likely((air - candidate - HAC1_DOWN_BAND_K) / spread) * harm_down)
        candidates = [c for c in range(math.floor(air - 2 * spread) - 1, math.floor(air + 2 * spread) + 2)
                      if floor_sp <= c <= SETPOINT_MAX]
        if not candidates:
            return None
        best = min(candidates, key=risk)
        if self.setpoint in candidates and risk(self.setpoint) <= risk(best) + 0.02:
            best = self.setpoint  # fewest setpoint changes
        return "hold", int(best), None, air, "hold"

    def ha_block(self, state: dict, now: float) -> dict[str, object]:
        """The compact read-only block for Home Assistant (see afterheat_data); ``state`` is the controller snapshot."""
        self.control.telemetry.add_power(now, state.get("afterheat_power_w"))
        algorithm = self.control.algorithms.get("pid")
        return build_block(self.control, self.control.telemetry, state, state.get("afterheat_algorithm_forecast"),
                           self.prediction, getattr(algorithm, "parts", None), now)

    def _deactivate(self) -> None:
        super()._deactivate()
        self.control.restart()
        self.algo_wish = None

    # -------------------------------------------------------------- reporting
    def _plain_reason(self, inp: PilotInputs) -> str:
        if not self._steering() or self.control.decision is None:
            return super()._plain_reason(inp)
        if self.note and inp.now - self.note_at <= 2 * self.clock.period and self.note.startswith(
                ("HAC1 sommerspærre", "Trin ", "HAC1 fulgte", "Frostsikring: HAC1")):
            return self.note
        label = ALGORITHM_LABELS[self.control.name]
        text = f"{label}: {self.control.decision.why}"
        result = self.control.result
        if result is not None and result.text:
            text += f" · sikkerhed: {result.text}"
        where = f"luft {danish(float(inp.t2ah))} °C (mål {danish(float(inp.target))} °C)"
        return f"{text} · {where}"

    def _snapshot(self, inp: PilotInputs, state: str, next_tick: float | None,
                  reason: str | None) -> dict[str, object]:
        snapshot = super()._snapshot(inp, state, next_tick, reason)
        control = getattr(self, "control", None)
        if control is None:
            return snapshot  # (the pilot's own first snapshot, before this class is set up)
        active = state == "pi"
        name = control.name
        algorithm_steers = active and self._steering()
        if not active:
            fallback = "fallback"
        elif control.paused:
            fallback = "paused"
        else:
            fallback = None
        result = control.result if algorithm_steers else None
        snapshot.update({
            "afterheat_algorithm": name,
            "afterheat_algorithm_label": ALGORITHM_LABELS[name],
            "afterheat_algorithm_active": name if algorithm_steers else (ALGORITHM_DEFAULT if active else None),
            "afterheat_algorithm_code": (
                ALGORITHM_CODES[name] if algorithm_steers else (ALGORITHM_CODES[ALGORITHM_DEFAULT] if active else None)),
            "afterheat_algorithm_wanted": (round(self.algo_wish, 2) if algorithm_steers and self.algo_wish is not None
                                           else None),
            "afterheat_algorithm_why": control.decision.why if algorithm_steers and control.decision else None,
            "afterheat_algorithm_state": fallback,
            "afterheat_safety_override": result.override if result is not None else None,
            "afterheat_model": control.status(),
            "afterheat_algorithm_forecast": (list(control.decision.forecast)
                                             if algorithm_steers and control.decision is not None else None),
            # What the standard pilot itself has learned: K per level step (reference fan) and fall overruns.
            "afterheat_pilot_learned": {
                "step_k": [round(self.heat.step(n), 2) for n in range(4)], "steps_measured": self.heat.measured,
                "fall_overrun_s": {str(k * 5): round(v) for k, v in sorted(self.fall_overrun.items())}},
        })
        if algorithm_steers and self.algo_wish is not None:
            snapshot["afterheat_level_wanted"] = round(self.algo_wish, 1)
        return snapshot


def _fan(value: object) -> float:
    return clamp(num(value) or 36.0, 10.0, 100.0)


def _short(value: float) -> str:
    return f"{value:.0f}" if float(value).is_integer() else f"{value:.2f}"


class DirectValveController:
    """A stepless 0-10 V valve (0-100 %) driven by the Pi, one decision per ``spec.period``.

    Not in hardware yet: used by the tests and the simulator. The standard pilot works
    through HAC1's setpoint only, so "standard" is not offered here.
    """

    def __init__(self, algorithm: str, spec: OutputSpec = DIRECT_SPEC, store_path: str | Path | None = None) -> None:
        if algorithm not in ALGORITHMS or algorithm == ALGORITHM_DEFAULT:
            raise ValueError(f"{algorithm!r} cannot drive a direct valve")
        self.control = AlgorithmControl(spec, store_path)
        self.control.name = algorithm
        self.next_at: float | None = None
        self.output: float | None = None
        self.result: dict[str, object] = {}

    def update(self, *, now: float, air: float, output: float, target: float, fan: float | None = None,
               water: float | None = None, air_in: float | None = None, frost_lock: bool = False,
               frost_floor: float = 18.0, trend: float | None = None) -> dict[str, object]:
        """``output`` is the opening in force (0-100 %); the result holds the opening to send."""
        level = clamp(float(output) / 10.0, 0.0, float(LEVEL_MAX))
        control = self.control
        control.observe(now, air, level, fan, water, air_in, target)
        if self.next_at is not None and now < self.next_at:
            return self.result
        self.next_at = now + control.spec.period
        low = 1.0 if frost_lock else 0.0
        view = ControlView(now=now, target=float(target), air=float(air), output=level, low=low,
                           high=float(LEVEL_MAX), spec=control.spec, model=control.model, fan=_fan(fan),
                           water=num(water), air_in=num(air_in), frost_lock=frost_lock,
                           frost_floor=float(frost_floor), trend=trend)
        chosen = control.decide(view, self._facts(view))
        if chosen is None:
            # Fault: hold the opening, still under the safety layer (the frost lock, the soft floor).
            decision, safety = Decision(level, "fejl i styringen; holder åbningen"), control.safety.apply(
                view, level, self._facts(view))
        else:
            decision, safety = chosen
        self.output = safety.output
        self.result = {
            "output_percent": round(safety.output * 10.0, 1), "wanted_percent": round(decision.output * 10.0, 1),
            "override": safety.override, "why": decision.why, "algorithm": control.name,
            "algorithm_code": ALGORITHM_CODES[control.name],
        }
        control.save(now)
        return self.result

    @staticmethod
    def _facts(view: ControlView) -> SafetyFacts:
        if not view.frost_lock:
            return SafetyFacts()
        floor = math.ceil(view.frost_floor) + FROST_MARGIN_K
        model = view.model
        scale, base = model.scale(view.fan, view.water, view.air_in), model.base(view.air_in)
        lower = max(view.low, view.output - view.spec.max_move)
        return SafetyFacts(frost_raise=view.air < floor or model.steady(view.output, scale, base) < floor,
                           frost_no_lower=model.steady(lower, scale, base) < floor + 0.5)


__all__ = ["AlgorithmControl", "AlgorithmPilot", "DirectValveController"]
