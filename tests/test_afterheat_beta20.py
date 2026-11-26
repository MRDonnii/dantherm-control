"""1.13.0-beta.20: the lift per step fitted over the pulse train, and the floor by the measured fall.

Live 2026-11-26 (beta.19, 50 % fan, soft/hard floor 0.5/0.8 K) the air went to 20.6-20.8 C
three times with the hard floor at 21.2 C: the model believed step 1 lifts ~2.1 K (~0.1 K
real), so it expected ~0.45 K a minute at step 0/1 where the air fell 1-1.7 K.
"""
import sys
import unittest
from collections import deque
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "gateway"), str(ROOT / "tests")]

from afterheat_control import (HAC1_SPEC, OVERRIDE_FLOOR, OVERRIDE_RATE, PRIOR_LIFT, CoilModel,  # noqa: E402
                               ControlView, OutputSpec, SafetyLayer)
from afterheat_sim import RLV2_LIVE, RLV2_LIVE_LIFT_AT_36, Hac1Plant  # noqa: E402

# The stored model live at 16:00 (afterheat_algorithms.json, beta.19).
LIVE_MODEL = {"version": 1, "lift": [0.0, 2.1282, 8.6834] + [8.8525] * 8, "tau_up": 83.72, "tau_down": 230.6,
              "dead": 36.23, "fan_exp": 0.0, "offset": -0.703, "air_in": 18.88}


def live_view(model, *, air, output, trend_k_min, now=10_000.0):
    return ControlView(now=now, target=22.0, air=air, output=float(output), low=0.0, high=10.0, spec=HAC1_SPEC,
                       model=model, fan=50.0, water=None, air_in=17.8, trend=trend_k_min / 60.0,
                       soft_floor_k=0.5, hard_floor_k=0.8, warm_bias_k=0.0)


def warm(model, *, air, output, now=10_000.0):
    """The model's state as live: tracking the air, the output in force for a while."""
    model.outputs = deque([(now - 600.0, float(output))])
    model.x, model.updated = air, now
    return model


class LiveDipTests(unittest.TestCase):
    def setUp(self):
        self.model = CoilModel.from_dict(LIVE_MODEL)

    def test_step_0_at_23_3_falling_1_5_k_a_minute_raises_even_under_the_rate_limit(self):
        # 16:28:46 / 16:44:50: HAC1 took step 1 -> 0 at ~23.3 C, the MPC wanted step 1 back,
        # the rate limit kept step 0 (code 4) and the air went on to 20.6-20.8 C.
        safety = SafetyLayer()
        for k in range(6):
            safety.observe(10_000.0 - 500 + 60 * k, float(k % 2), 60.17)
        safety.observe(10_000.0 - 60, 0.0, 60.17)
        view = live_view(warm(self.model, air=22.8, output=0), air=22.8, output=0, trend_k_min=-1.5)
        result = safety.apply(view, 1.0)
        self.assertEqual(result.output, 1.0)
        self.assertNotEqual(result.override, OVERRIDE_RATE)
        # The MPC keeping step 0 is overruled by the measured fall alone.
        result = SafetyLayer().apply(replace(view, now=view.now + 1), 0.0)
        self.assertEqual((result.output, result.override), (1.0, OVERRIDE_FLOOR))

    def test_step_1_at_22_falling_1_k_a_minute_gets_step_2_although_a_raise_is_on_its_way(self):
        # 17:06:53: step 0 -> 1 a tick before, the air at ~21.6 C and falling; step 1 was kept
        # (a raise "on its way") and the air went to 20.8 C.
        safety = SafetyLayer()
        safety.observe(10_000.0 - 120, 0.0, 60.17)
        safety.observe(10_000.0 - 60, 1.0, 60.17)
        model = CoilModel.from_dict(LIVE_MODEL)
        view = live_view(warm(model, air=22.0, output=1), air=22.0, output=1, trend_k_min=-1.0)
        self.assertEqual(safety.apply(view, 1.0).output, 2.0)

    def test_no_step_down_to_0_at_23_3_falling_1_5_k_a_minute(self):
        safety = SafetyLayer()
        view = live_view(warm(self.model, air=23.3, output=1), air=23.3, output=1, trend_k_min=-1.5)
        self.assertTrue(safety.lower_blocked(view))
        result = safety.apply(view, 0.0)
        # (Never less; on this fall step 1 is not even enough: step 2 at once.)
        self.assertGreaterEqual(result.output, 1.0)
        self.assertEqual(result.override, OVERRIDE_FLOOR)
        # Warm and only slowly falling, the measured fall does not stop it.
        slow = live_view(warm(self.model, air=24.5, output=1), air=24.5, output=1, trend_k_min=-0.2)
        self.assertFalse(SafetyLayer().lower_blocked(slow))

    def test_the_old_step_1_is_reset_on_load_and_a_fitted_one_is_kept(self):
        self.assertEqual(self.model.lift[1], PRIOR_LIFT[1])
        self.assertEqual(self.model.lift[2], 8.6834)
        saved = self.model.to_dict()
        saved["lift"][1] = 0.4
        self.assertEqual(CoilModel.from_dict(saved).lift[1], 0.4)


class LiftIdentificationTests(unittest.TestCase):
    def test_the_lift_per_step_and_the_fall_at_step_0_1_come_from_a_one_tick_pulse_train(self):
        # The live pattern (step 1 kept only a tick or two between pulses on step 2), open
        # loop on the live coil, learned from the wrong live model of 2026-11-26.
        plant = Hac1Plant(**{**RLV2_LIVE, "fan": 50.0}, level=1, seed=4, setpoint=22.0)
        plant.ignore_setpoint = True
        model = CoilModel.from_dict({**LIVE_MODEL, "lift": [0.0, 2.4, 8.6834] + [8.8525] * 8, "lift_fit": True})
        self.assertEqual(model.lift[1], 2.4)
        pattern = [1, 2, 1, 0, 1, 2, 2, 1, 1, 0, 1, 2, 1, 0, 0, 1, 2, 1]
        start, pending, falls = plant.now, [], []
        spec = OutputSpec(period=60.0, apply_delay_s=0.0)
        while plant.now - start < 150 * 60:
            plant.level = pattern[int((plant.now - start) // 60.17) % len(pattern)]
            plant.step(1.0)
            if int(plant.now) % 3:
                continue
            model.observe(plant.now, plant.raw, float(plant.level), plant.fan, plant.water(), plant.air_in())
            if plant.now - start > 100 * 60 and int(plant.now) % 30 == 0 and plant.level <= 1:
                path = model.simulate(plant.now, [float(plant.level)], spec, model.last_scale,
                                      model.base(plant.air_in()), step_s=15.0)
                pending.append((plant.now + 60, plant.x, path[-1] - model.x))
            while pending and plant.now >= pending[0][0]:
                _due, was, predicted = pending.pop(0)
                if plant.x - was < -0.3:
                    falls.append((plant.x - was, predicted))
        self.assertGreaterEqual(model.reid_accepted, 1)
        scale = model.scale(plant.fan, plant.water(), plant.air_in())
        self.assertLess(scale * model.lift[1], 0.8)  # (the coil: ~0.1 K; believed 2.4 K)
        self.assertAlmostEqual(scale * model.lift[2], plant.rise(2), delta=0.2 * plant.rise(2))
        real = sum(r for r, _p in falls) / len(falls)
        predicted = sum(p for _r, p in falls) / len(falls)
        self.assertGreater(len(falls), 20)
        self.assertAlmostEqual(predicted / real, 1.0, delta=0.25)


if __name__ == "__main__":
    unittest.main()
