"""Selectable afterheat algorithms: safety layer first, default unchanged, every algorithm sane and persisted."""
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "gateway"), str(ROOT / "tests")]

from afterheat_actuators import AlgorithmControl, AlgorithmPilot, DirectValveController  # noqa: E402
from afterheat_algorithms import ALGORITHM_CLASSES, Algorithm, MpcAlgorithm  # noqa: E402
from afterheat_control import (DIRECT_SPEC, HAC1_SPEC, OVERRIDE_FAULT, OVERRIDE_FLOOR, OVERRIDE_FROST,  # noqa: E402
                               HARD_FLOOR_DEFAULT_K, OVERRIDE_MOVE, SOFT_FLOOR_DEFAULT_K, CoilModel, ControlView,
                               Decision, SafetyFacts, SafetyLayer)
from afterheat_pilot import AfterheatPilot  # noqa: E402
from afterheat_sim import DirectValvePlant, Hac1Plant, RLV2_VALVES, run, run_direct  # noqa: E402
from history_analysis import algorithm_performance, row_matches  # noqa: E402

NOW = 1_000_000.0


def warm_model(air: float = 22.0, output: float = 1.0) -> CoilModel:
    model = CoilModel()
    for k in range(40):
        model.observe(NOW - 400 + 10 * k, air, output, 36.0, 44.0, 17.9)
    return model


def view(model: CoilModel, *, air: float = 22.0, output: float = 1.0, spec=HAC1_SPEC, low: float = 0.0,
         target: float = 22.0) -> ControlView:
    return ControlView(now=NOW, target=target, air=air, output=output, low=low, high=10.0, spec=spec, model=model,
                       fan=36.0, water=44.0, air_in=17.9)


class SafetyLayerWins(unittest.TestCase):
    def test_move_limit_and_range(self):
        model = warm_model(air=26.0, output=2.0)
        result = SafetyLayer().apply(view(model, air=26.0, output=2.0), 9.0)
        self.assertEqual(result.output, 3.0)
        self.assertEqual(result.override, OVERRIDE_MOVE)
        self.assertEqual(SafetyLayer().apply(view(model, air=26.0, output=2.0), float("nan")).override, OVERRIDE_FAULT)

    def test_soft_floor_raises_and_never_lowers_when_cold(self):
        model = warm_model(air=21.0, output=1.0)
        for wanted in (0.0, 1.0):
            result = SafetyLayer().apply(view(model, air=21.0, output=1.0), wanted)
            self.assertEqual(result.output, 2.0)
            self.assertEqual(result.override, OVERRIDE_FLOOR)

    def test_frost_lock_beats_everything(self):
        model = warm_model(air=24.0, output=2.0)
        safety = SafetyLayer()
        lowered = safety.apply(view(model, air=24.0, output=2.0, low=1.0), 1.0, SafetyFacts(frost_no_lower=True))
        self.assertEqual((lowered.output, lowered.override), (2.0, OVERRIDE_FROST))
        raised = safety.apply(view(model, air=24.0, output=2.0, low=1.0), 1.0, SafetyFacts(frost_raise=True))
        self.assertEqual((raised.output, raised.override), (3.0, OVERRIDE_FROST))

    def test_a_broken_algorithm_falls_back_and_pauses(self):
        class Broken(Algorithm):
            name = "mpc"

            def decide(self, v):
                raise RuntimeError("boom")

        control = AlgorithmControl(HAC1_SPEC)
        control.name = "mpc"
        control.algorithms["mpc"] = Broken()
        model = warm_model()
        control.model = model
        for _ in range(3):
            self.assertIsNone(control.decide(view(model), SafetyFacts()))
        self.assertTrue(control.paused)


class DefaultUnchanged(unittest.TestCase):
    def test_standard_is_the_pilot_byte_for_byte(self):
        seen = []
        for pilot in (AfterheatPilot(), AlgorithmPilot()):
            rows = run(pilot, Hac1Plant(**RLV2_VALVES, level=1, seed=5), target=22.0, minutes=40, sensors=True)
            seen.append([(r["setpoint"], r["level"], r["action"], r["reason"], r["wanted"]) for r in rows])
        self.assertEqual(seen[0], seen[1])


class EveryAlgorithm(unittest.TestCase):
    def test_hac1_outputs_are_whole_levels_one_step_a_tick(self):
        for name in ALGORITHM_CLASSES:
            with self.subTest(name=name):
                pilot = AlgorithmPilot()
                rows = run(pilot, Hac1Plant(**RLV2_VALVES, level=1, seed=3), target=22.0, minutes=25,
                           algorithm=name, sensors=True)
                self.assertEqual(pilot.result["afterheat_algorithm_active"], name)
                levels = [r["level"] for r in rows]
                self.assertTrue(all(0 <= level <= 10 for level in levels))
                self.assertTrue(all(abs(b - a) <= 1 for a, b in zip(levels, levels[1:])))
                self.assertGreater(min(r["air"] for r in rows[300:]), 20.0)

    def test_direct_valve_outputs_stay_in_range(self):
        for name in ALGORITHM_CLASSES:
            with self.subTest(name=name):
                rows = run_direct(DirectValveController(name), DirectValvePlant(**RLV2_VALVES, level=1, seed=3),
                                  target=22.0, minutes=25, sensors=True)
                outputs = [r["level"] * 10 for r in rows]
                self.assertTrue(all(0 <= value <= 100 for value in outputs))
                self.assertTrue(all(abs(b - a) <= DIRECT_SPEC.max_move * 10 + 1e-6 for a, b in zip(outputs, outputs[1:])))
        with self.assertRaises(ValueError):
            DirectValveController("standard")

    def test_learning_is_saved_and_restored(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "afterheat_algorithms.json"
            controller = DirectValveController("pid", store_path=path)
            run_direct(controller, DirectValvePlant(**RLV2_VALVES, level=1, seed=3), target=22.0, minutes=20,
                       sensors=True)
            for name in ALGORITHM_CLASSES:
                controller.control.algorithm(name)
            controller.control.save(0.0, force=True)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(set(saved["algorithms"]), set(ALGORITHM_CLASSES))
            restored = AlgorithmControl(DIRECT_SPEC, path)
            self.assertEqual(restored.model.lift, CoilModel.from_dict(saved["model"]).lift)
            self.assertEqual(restored.algorithm("pid").to_dict(), controller.control.algorithm("pid").to_dict())
            for name in ALGORITHM_CLASSES:
                restored.algorithm(name).load(saved["algorithms"][name])
        broken = CoilModel.from_dict({"version": 1, "lift": [0, 5, 1] + [0] * 8, "tau_up": 1, "tau_down": 1, "dead": 1})
        self.assertEqual(broken.lift, CoilModel().lift)
        for name, cls in ALGORITHM_CLASSES.items():
            cls().load({"gain": "x", "tune": {"20.0": [math.nan, 1, 1]}, "trim": None})


class Diagnostics(unittest.TestCase):
    def test_performance_per_algorithm(self):
        now = 1_800_000_000.0
        rows = []
        for i in range(120):
            code = 0 if i < 60 else 1
            air = 22.4 if code == 0 else (21.3 if i % 10 == 0 else 22.1)
            rows.append({"ts": now - 7200 + 60 * i, "heating_coil_after_temperature": air, "afterheat_target": 22.0,
                         "afterheat_level": 1 + (i % 4 == 0), "afterheat_algorithm_code": code,
                         "afterheat_safety_override": 5 if i == 70 else 0, "afterheat_context": 0})
        result = algorithm_performance(rows, now)
        day = {entry["algorithm"]: entry for entry in result["24h"]["algorithms"]}
        self.assertTrue(result["24h"]["compare"])
        self.assertEqual(day["standard"]["under_floor_percent"], 0.0)
        self.assertEqual(day["mpc"]["under_floor_percent"], 10.0)
        self.assertEqual(day["mpc"]["air_min"], 21.3)
        self.assertEqual(day["mpc"]["safety_minutes"], 1)
        self.assertGreater(day["mpc"]["level_changes_per_hour"], 20)
        self.assertEqual([e["algorithm"] for e in result["1h"]["algorithms"]], ["mpc"])
        self.assertTrue(row_matches(rows[-1], "algo:mpc"))
        self.assertFalse(row_matches(rows[0], "algo:mpc"))


class MpcTakesTurnsSlowly(unittest.TestCase):
    """Live 2026-11-25: the MPC took turns 1<->2 every tick (39.5 changes an hour, the air +1.7 K warm)."""

    def test_a_level_is_kept_its_minimum_dwell_but_not_one_hac1_took_by_itself(self):
        model = warm_model(air=24.0, output=2.0)
        model.observe(NOW - 40, 24.5, 1.0, 36.0, 44.0, 17.9)  # 2 -> 1 shown 40 s ago (the tick ~55 s ago)
        mpc = MpcAlgorithm()
        mpc.note_sent(1.0, NOW - 55)  # ... as asked
        here = view(model, air=24.5, output=1.0)
        self.assertEqual(mpc.dwell_left(here), 1)
        # The dwell holds back a turn back up; going on down (a ramp 2 -> 1 -> 0) is no turn.
        self.assertTrue(all(plan[0] <= 1.0 for plan in mpc._discrete_plans(here, mpc.HORIZON_TICKS)))
        self.assertEqual(mpc.decide(here).output, 1.0)
        mpc.note_sent(2.0, NOW - 55)  # the Pi asked to hold 2: HAC1 stepped down by itself
        self.assertEqual(mpc.dwell_left(here), 0)

    def test_duty_cycle_on_the_rlv2_coil_without_per_tick_turns(self):
        # The levels as asked (a direct actuator, no HAC1 hold window): step 1 alone settles
        # below the floor at 36 % fan and step 2 ~6 K over, so it must take turns.
        rows = run_direct(DirectValveController("mpc", spec=HAC1_SPEC), DirectValvePlant(**RLV2_VALVES, level=1, seed=3),
                          target=22.0, minutes=80, sensors=True)
        kept = [r for r in rows if r["t"] >= 20 * 60]
        ticks, next_at = [], kept[0]["t"]
        for r in kept:
            if r["t"] >= next_at:
                ticks.append(r["level"])
                next_at += HAC1_SPEC.period
        visits, run_length = [], 1
        for a, b in zip(ticks, ticks[1:]):
            if a == b:
                run_length += 1
            else:
                visits.append((a, run_length))
                run_length = 1
        changes = len(visits)
        self.assertGreater(changes, 4)  # it does take turns
        self.assertLess(changes / ((kept[-1]["t"] - kept[0]["t"]) / 3600), 30)
        # The lower level of a turn is never visited for a single tick.
        lows = [length for k, (level, length) in enumerate(visits[1:-1], 1)
                if visits[k - 1][0] > level < visits[k + 1][0]]
        self.assertTrue(lows and min(lows) >= MpcAlgorithm.MIN_DWELL_TICKS, visits)
        errors = [r["air"] - 22.0 for r in kept]
        mean = sum(errors) / len(errors)
        self.assertTrue(0.0 <= mean <= 1.6, mean)
        # Down to the soft floor is allowed now and then; the hard floor never.
        self.assertLess(sum(e < -SOFT_FLOOR_DEFAULT_K for e in errors) / len(errors), 0.03)
        self.assertGreater(min(errors), -HARD_FLOOR_DEFAULT_K)


if __name__ == "__main__":
    unittest.main()


class MpcLiveAb20261126(unittest.TestCase):
    """Live A/B 2026-11-26 (36 % fan, RLV 2 turns): HAC1 alone steadier than the MPC (spread 1.27 against 1.62 K)."""

    def test_level_zero_under_the_frost_lock_only_with_a_warm_coil(self):
        from types import SimpleNamespace
        warm = SimpleNamespace(frost_lock=True, before_heater=17.8, frost_sensor=21.0)
        cold = SimpleNamespace(frost_lock=True, before_heater=10.0, frost_sensor=21.0)
        self.assertEqual(AfterheatPilot.frost_min_level(warm), 0)
        self.assertEqual(AfterheatPilot.frost_min_level(cold), 1)
        model = warm_model(air=23.0, output=1.0)
        mpc = MpcAlgorithm()
        for low, lowest in ((0.0, 0.0), (1.0, 1.0)):
            plans = mpc._discrete_plans(view(model, air=23.0, output=1.0, low=low), mpc.HORIZON_TICKS)
            self.assertEqual(min(min(plan) for plan in plans), lowest)
        # HAC1's own staircase (a step a tick up to 2, kept, down to 0, kept) is among the plans
        stairs = mpc._staircase_plans(view(model, air=23.0, output=1.0), mpc.HORIZON_TICKS)
        self.assertIn((1.0, 2.0, 2.0, 1.0, 0.0, 0.0, 1.0, 2.0, 2.0, 1.0, 0.0, 0.0), stairs)

    def test_a_pulse_on_its_way_gets_no_step_on_top_and_may_end(self):
        model = warm_model(air=21.8, output=1.0)
        model.observe(NOW - 40, 21.4, 2.0, 36.0, 44.0, 17.9)  # 1 -> 2 shown 40 s ago: still in the dead time
        model.observe(NOW - 5, 21.3, 2.0, 36.0, 44.0, 17.9)
        pending = SafetyLayer()
        pending.observe(NOW - 400, 1.0, HAC1_SPEC.period, 21.8, 22.0)
        pending.observe(NOW - 40, 2.0, HAC1_SPEC.period, 21.4, 22.0)
        here = view(model, air=21.3, output=2.0)
        self.assertEqual(pending.apply(here, 2.0).output, 2.0)  # was: the floor added step 3
        self.assertEqual(pending.apply(here, 1.0).output, 1.0)  # the one-tick pulse may end
        fresh = SafetyLayer()
        fresh.observe(NOW - 400, 1.0, HAC1_SPEC.period, 21.8, 22.0)
        self.assertEqual(fresh.apply(here, 1.0).output, 3.0)  # no raise of its own on its way: the floor raises

    def test_mpc_on_the_live_rlv2_coil_beats_hac1_alone(self):
        from afterheat_sim import RLV2_LIVE
        plant = Hac1Plant(**RLV2_LIVE, level=1, seed=3, setpoint=22.0)

        def sensors(inp, _plant, _gateway):
            inp.before_heater, inp.frost_sensor = _plant.air_in(), 22.0
        rows = run(AlgorithmPilot(), plant, target=22.0, minutes=150, algorithm="mpc", frost_lock=True,
                   sensors=True, inputs_hook=sensors)
        rows = [r for r in rows if r["t"] >= 20 * 60 and int(r["t"]) % 60 < 2]
        errors = [r["air"] - 22.0 for r in rows]
        mean = sum(errors) / len(errors)
        spread = math.sqrt(sum((e - mean) ** 2 for e in errors) / len(errors))
        under = 100 * sum(e < -0.5 for e in errors) / len(errors)
        changes = sum(a["level"] != b["level"] for a, b in zip(rows, rows[1:])) / (len(rows) / 60)
        # HAC1 alone live: spread 1.27 K, 24 % under target - 0.5 K, 36 changes an hour.
        self.assertLess(spread, 1.27)
        self.assertLess(under, 10.0)
        self.assertLessEqual(changes, 36.0)
