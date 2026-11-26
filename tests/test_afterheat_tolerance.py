"""The owner's afterheat tolerance: soft floor, hard floor and warm bias (1.13.0-beta.19)."""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "gateway"), str(ROOT / "tests")]

import controller_core  # noqa: E402
from afterheat_actuators import AlgorithmPilot  # noqa: E402
from afterheat_algorithms import tracking_cost  # noqa: E402
from afterheat_control import HARD_FLOOR_DEFAULT_K, SOFT_FLOOR_DEFAULT_K, WARM_BIAS_K  # noqa: E402
from afterheat_sim import RLV2_LIVE, Hac1Plant, run  # noqa: E402
from controller_core import ControllerEngine, ControllerError, ControllerState, HardwareAdapter  # noqa: E402

KEYS = ("afterheat_soft_floor_k", "afterheat_hard_floor_k", "afterheat_warm_bias_k")


def live_coil(pilot, minutes=150, fan=36.0, algorithm="mpc"):
    plant = Hac1Plant(**{**RLV2_LIVE, "fan": fan}, level=1, seed=3, setpoint=22.0)

    def sensors(inp, _plant, _gateway):
        inp.before_heater, inp.frost_sensor = _plant.air_in(), 22.0
    rows = run(pilot, plant, target=22.0, minutes=minutes, algorithm=algorithm, frost_lock=True, sensors=True,
               inputs_hook=sensors)
    return [r for r in rows if r["t"] >= 20 * 60]


class SettingsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        patcher = mock.patch.object(controller_core, "AFTERHEAT_OVERRIDE_PATH", self.dir / "override.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.state = ControllerState(self.dir / "controller.json")

    def test_defaults_validation_and_they_reach_the_algorithms(self):
        self.assertEqual([self.state.data[key] for key in KEYS], [0.6, 1.0, 0.3])
        for bad in ({"afterheat_soft_floor_k": 0.1}, {"afterheat_hard_floor_k": 3.0},
                    {"afterheat_warm_bias_k": -0.1}, {"afterheat_warm_bias_k": "lidt"},
                    {"afterheat_soft_floor_k": 1.0},  # not under the hard floor (1.0)
                    {"afterheat_soft_floor_k": 0.8, "afterheat_hard_floor_k": 0.7}):
            with self.subTest(**bad), self.assertRaises(ControllerError):
                self.state.configure(bad)
        self.assertEqual([self.state.data[key] for key in KEYS], [0.6, 1.0, 0.3])
        self.state.configure({"afterheat_soft_floor_k": 0.5, "afterheat_hard_floor_k": 0.9,
                              "afterheat_warm_bias_k": 0.2})
        self.assertEqual([self.state.snapshot()[key] for key in KEYS], [0.5, 0.9, 0.2])
        self.assertTrue(all(key in ControllerState.ADVANCED_LABELS for key in KEYS))

        engine = ControllerEngine(self.state, HardwareAdapter(
            set_afterheat_setpoint=lambda *_: None, set_fireplace=lambda *_: None,
            set_standby=lambda *_: None, write_fan_pair=lambda *_: None))
        now = time.time()
        engine.set_afterheat_inputs({"level": 2, "level_at": now - 1, "level_changed_at": None, "t2ah": 21.0,
                                     "t2ah_at": now - 1, "t2ah_raw": 21.0, "fan_supply": 36, "master": True})
        engine.resolve(now)
        self.assertEqual(engine.afterheat_pilot.control.tolerance,
                         {"soft_floor_k": 0.5, "hard_floor_k": 0.9, "warm_bias_k": 0.2})

    def test_a_saved_soft_floor_at_or_over_the_hard_floor_falls_back(self):
        path = self.dir / "saved.json"
        path.write_text(json.dumps({"afterheat_soft_floor_k": 1.2, "afterheat_hard_floor_k": 0.8}))
        data = ControllerState(path).data
        self.assertEqual((data["afterheat_soft_floor_k"], data["afterheat_hard_floor_k"]),
                         (SOFT_FLOOR_DEFAULT_K, HARD_FLOOR_DEFAULT_K))

    def test_odd_values_given_to_the_actuator_fall_back_and_the_hard_floor_is_never_over_the_soft(self):
        pilot = AlgorithmPilot()
        pilot.control.set_tolerance(None, "x", float("nan"))
        self.assertEqual(pilot.control.tolerance, {"soft_floor_k": SOFT_FLOOR_DEFAULT_K,
                                                   "hard_floor_k": HARD_FLOOR_DEFAULT_K, "warm_bias_k": WARM_BIAS_K})
        pilot.control.set_tolerance(0.9, 0.5, 0.3)
        self.assertEqual(pilot.control.tolerance["hard_floor_k"], 0.9)


class CostTests(unittest.TestCase):
    def test_cheap_to_the_soft_floor_steep_to_the_hard_floor_prohibitive_under_it(self):
        at_soft = tracking_cost(-0.6)
        between = tracking_cost(-0.8)
        at_hard = tracking_cost(-1.0)
        under = tracking_cost(-1.2)
        self.assertLess(at_soft, 0.5)  # a dip to 21.4 C at 22 C hardly counts
        self.assertGreater(between, 10 * at_soft)
        self.assertGreater(under - at_hard, 3 * (at_hard - at_soft))  # 0.2 K under costs more than the 0.4 K above
        # The settings move the floors.
        self.assertLess(tracking_cost(-0.8, soft=0.9, hard=1.3), 1.0)
        self.assertGreater(tracking_cost(-0.5, soft=0.3, hard=0.5), tracking_cost(-0.5))
        # Warm: nearly free up to the warm bias, then it counts.
        self.assertLess(tracking_cost(0.3), 0.02)
        self.assertGreater(tracking_cost(0.6, warm=0.0), 2 * tracking_cost(0.6))


class LiveCoilTests(unittest.TestCase):
    def test_the_mpc_keeps_the_hard_floor_on_the_live_coil(self):
        rows = live_coil(AlgorithmPilot())
        errors = [r["air"] - 22.0 for r in rows]
        self.assertGreater(min(errors), -HARD_FLOOR_DEFAULT_K - 0.15)
        self.assertLess(sum(e < -HARD_FLOOR_DEFAULT_K for e in errors) / len(errors), 0.01)
        mean = sum(errors) / len(errors)
        self.assertLess(mean, 1.3)  # the A/B 2026-11-26 had +1.65 K

        # Stricter settings: higher troughs (and a warmer mean).
        strict = AlgorithmPilot()
        strict.control.set_tolerance(0.3, 0.6, 0.3)
        tight = [r["air"] - 22.0 for r in live_coil(strict)]
        self.assertGreater(min(tight), min(errors))
        self.assertGreater(min(tight), -0.6 - 0.3)

    def test_the_standard_pilot_ignores_the_tolerance(self):
        default = live_coil(AlgorithmPilot(), minutes=60, algorithm="standard")
        other = AlgorithmPilot()
        other.control.set_tolerance(0.3, 0.5, 0.0)
        self.assertEqual(live_coil(other, minutes=60, algorithm="standard"), default)


if __name__ == "__main__":
    unittest.main()
