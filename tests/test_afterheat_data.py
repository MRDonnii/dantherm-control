"""The read-only afterheat block for Home Assistant: kW/kWh, midnight reset, restore, block shape."""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "gateway"), str(ROOT / "tests")]

from afterheat_actuators import AlgorithmControl, AlgorithmPilot  # noqa: E402
from afterheat_control import HAC1_SPEC  # noqa: E402
from afterheat_data import Telemetry, build_block, mean_abs_error  # noqa: E402
from afterheat_sim import Hac1Plant, RLV2_VALVES, run  # noqa: E402

NOON = time.mktime((2026, 11, 25, 12, 0, 0, 0, 0, -1))


class Energy(unittest.TestCase):
    def test_kwh_is_power_times_time_and_repeated_calls_do_not_double(self):
        t = Telemetry()
        t.add_power(NOON, 2000.0)
        t.add_power(NOON + 0.2, 2000.0)  # a second snapshot a moment later: ignored
        for minute in range(1, 31):
            t.add_power(NOON + 60 * minute, 2000.0)
        self.assertAlmostEqual(t.energy_today(NOON + 1800), 1.0, places=2)  # 2 kW for 30 min

    def test_unknown_power_and_gaps_add_nothing_and_midnight_resets(self):
        t = Telemetry()
        t.add_power(NOON, None)
        t.add_power(NOON + 60, 1000.0)
        t.add_power(NOON + 120, 1000.0)
        self.assertAlmostEqual(t.energy_today(NOON + 120), 1000 / 1000 / 60, places=3)
        t.add_power(NOON + 3600, 1000.0)  # a long hole is not bridged
        self.assertAlmostEqual(t.energy_today(NOON + 3600), 1 / 60, places=3)
        midnight = NOON + 12.5 * 3600
        t.add_power(midnight, 1000.0)
        self.assertEqual(t.energy_today(midnight), 0.0)

    def test_survives_a_restart_through_the_store(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "store.json"
            control = AlgorithmControl(HAC1_SPEC, path)
            control.telemetry.add_power(NOON, 3000.0)
            control.telemetry.add_power(NOON + 60, 3000.0)
            control.save(NOON, force=True)
            self.assertIn("energy", json.loads(path.read_text()))
            again = AlgorithmControl(HAC1_SPEC, path)
            self.assertAlmostEqual(again.telemetry.energy_today(NOON + 60), 0.05, places=3)


class Block(unittest.TestCase):
    def test_block_has_plain_numbers_and_none_when_unknown(self):
        pilot = AlgorithmPilot()
        run(pilot, Hac1Plant(**RLV2_VALVES, level=1, seed=3), target=22.0, minutes=25, algorithm="pid", sensors=True)
        now = time.time()
        state = {"afterheat_level": 3, "afterheat_algorithm_active": "pid", "afterheat_algorithm_label": "PID",
                 "afterheat_power_w": 1500.0, "afterheat_target": 22.0, "heating_coil_after_temperature": 21.5,
                 "afterheat_algorithm_forecast": None}
        block = pilot.ha_block(state, now)
        json.dumps(block)  # plain, serialisable
        self.assertEqual(block["algorithm"], "pid")
        self.assertEqual(block["heat_kw"], 1.5)
        self.assertEqual(block["pid"]["error_k"], 0.5)
        self.assertIn(block["model_state"], ("learning", "ready", "uncertain"))
        self.assertIsNotNone(block["step_k"])
        self.assertIsNotNone(block["tau_up_s"])
        empty = AlgorithmPilot().ha_block({}, now)
        self.assertIsNone(empty["algorithm"])
        self.assertIsNone(empty["heat_kw"])
        self.assertIsNone(empty["step_k"])
        self.assertIsNone(empty["pid"])
        self.assertIsNone(empty["air_1min_c"])
        self.assertIsNone(empty["performance"]["1h"]["mean_error_k"])

    def test_performance_and_error_over_samples(self):
        t = Telemetry()
        for i in range(60):  # an hour: 1 K under target half the time, a level change every 10 min
            t.sample(NOON + 60 * i, 21.0 if i % 2 else 22.5, 22.0, float(i // 10))
        perf = t.performance(NOON + 3600)["1h"]
        self.assertEqual(perf["air_min_c"], 21.0)
        self.assertEqual(perf["under_percent"], 50.0)
        self.assertAlmostEqual(perf["mean_error_k"], -0.25, places=2)
        self.assertAlmostEqual(perf["changes_per_hour"], 5.1, places=1)
        self.assertIsNone(mean_abs_error(AlgorithmControl(HAC1_SPEC).model))


if __name__ == "__main__":
    unittest.main()
