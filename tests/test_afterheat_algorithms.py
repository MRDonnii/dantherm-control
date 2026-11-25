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
from afterheat_algorithms import ALGORITHM_CLASSES, Algorithm  # noqa: E402
from afterheat_control import (DIRECT_SPEC, HAC1_SPEC, OVERRIDE_FAULT, OVERRIDE_FLOOR, OVERRIDE_FROST,  # noqa: E402
                               OVERRIDE_MOVE, CoilModel, ControlView, Decision, SafetyFacts, SafetyLayer)
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


if __name__ == "__main__":
    unittest.main()
