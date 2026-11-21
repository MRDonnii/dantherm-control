"""Pi afterheat control (gateway/afterheat_pilot.py) against the fitted HAC1 plant simulator."""
import json
import logging
import math
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "gateway"))
sys.path.insert(0, str(ROOT / "tests"))

import afterheat_pilot as pilot_module
import afterheat_sim as sim
import controller_core
from afterheat_pilot import (
    AfterheatPilot, PilotInputs, TickClock, hac1_action, hac1_setpoint, hold_friendly,
    robust_temperature, setpoint_margin,
)
from controller_core import ControllerEngine, ControllerError, ControllerState, HardwareAdapter

START = 1_000_000.0
TICK = pilot_module.TICK_PERIOD_S


def settle_minutes(rows, target, start=0.0, end=None, band=0.5):
    """Minutes after ``start`` until the air stays within ``band`` (until ``end``)."""
    window = [r for r in rows if r["t"] >= start and (end is None or r["t"] < end)]
    outside = [r["t"] for r in window if abs(r["air"] - target) > band]
    return 0.0 if not outside else (max(outside) - start) / 60


class MappingTests(unittest.TestCase):
    def test_whole_degree_setpoints_make_hac1_take_the_action(self):
        for temperature in (19.0, 20.37, 21.5, 21.92, 22.04, 23.7):
            with self.subTest(temperature=temperature):
                self.assertEqual(hac1_action(hac1_setpoint("up", temperature), temperature), "up")
                self.assertEqual(hac1_action(hac1_setpoint("down", temperature), temperature), "down")
                hold = hac1_setpoint("hold", temperature)
                if hold is not None:
                    self.assertEqual(hac1_action(hold, temperature), "hold")

    def test_hold_is_impossible_just_below_a_whole_degree(self):
        self.assertIsNone(hac1_setpoint("hold", 21.95))
        self.assertEqual(hac1_setpoint("hold", 21.5), 21)
        self.assertEqual(hac1_setpoint("up", 21.75), 23)  # floor(21.75 + 0.3) + 1
        self.assertEqual(hac1_setpoint("down", 21.75), 20)  # floor(21.75 - 1.2)

    def test_hac1_ignores_the_fraction(self):
        self.assertEqual(hac1_action(21.9, 21.5), "hold")
        self.assertEqual(hac1_action(22.0, 21.5), "up")

    def test_margin(self):
        self.assertAlmostEqual(setpoint_margin(22, "up", 21.6), 0.4)
        self.assertAlmostEqual(setpoint_margin(20, "down", 21.2), 0.25)
        self.assertAlmostEqual(setpoint_margin(21, "hold", 21.3), 0.3)

    def test_target_is_kept_away_from_whole_degrees(self):
        self.assertEqual(hold_friendly(21.5), 21.5)
        self.assertAlmostEqual(hold_friendly(22.0), 22.15)
        self.assertAlmostEqual(hold_friendly(21.95), 21.78)
        self.assertAlmostEqual(hold_friendly(21.05), 21.15)
        for target in (17.0, 20.9, 21.03, 24.5):
            self.assertLessEqual(abs(hold_friendly(target) - target), 0.25)

    def test_robust_temperature_leaves_warm_glitches_out(self):
        self.assertAlmostEqual(robust_temperature([21.40, 21.44, 24.9, 21.42]), 21.42)
        self.assertIsNone(robust_temperature([]))
        self.assertIsNone(robust_temperature([None, "x"]))


class TickClockTests(unittest.TestCase):
    def test_learns_the_phase_from_observed_changes(self):
        clock = TickClock()
        tick0 = START + 7.0
        for k in (0, 1, 4, 9, 15):
            clock.observe(tick0 + k * clock.period + 2.0)  # seen up to 3 s late
        self.assertTrue(clock.known(tick0 + 15 * clock.period))
        predicted = clock.next_tick(tick0 + 20 * clock.period + 1.0)
        self.assertAlmostEqual(predicted, tick0 + 21 * clock.period, delta=2.0)

    def test_phase_expires_without_changes(self):
        clock = TickClock()
        clock.observe(START)
        self.assertTrue(clock.known(START + 600))
        self.assertFalse(clock.known(START + pilot_module.PHASE_VALID_S + 1))

    def test_jump_reanchors(self):
        clock = TickClock()
        clock.observe(START)
        clock.observe(START + 3 * clock.period + 30)  # e.g. HAC1 restarted
        self.assertAlmostEqual(clock.next_tick(START + 3 * clock.period + 31),
                               START + 4 * clock.period + 30 - pilot_module.OBSERVATION_LATENCY_S, delta=0.01)


class SimulatorFitTests(unittest.TestCase):
    def test_simulator_reproduces_the_logged_experiments(self):
        # Offset + one 40-minute swing stand in for the unknown air before the
        # coil and the Calefa flow temperature; the rest is the model's own.
        for name in ("hac1_exp_climb_to_10.csv", "hac1_exp_hold_level3_level1.csv",
                     "hac1_exp_level2_level4.csv"):
            with self.subTest(name=name):
                rms, swing = sim.replay_rms(sim.load_experiment(name))
                self.assertLess(rms, 0.4)
                self.assertLess(swing, 2.0)

    def test_simulator_fits_the_open_valve_trial(self):
        # 2026-11-20, valves opened: level 1 gave ~0.2 K, level 2 ~6 K (27 % fan).
        rows = sim.load_trial()
        self.assertEqual(len(rows), 314)
        fitted = sim.replay_trial(rows, sim.TRIAL_LEVEL_HEAT)
        linear = sim.replay_trial(rows, [1.25 * level for level in range(11)])
        concave = sim.replay_trial(rows, [0.8 * sim.level_curve(level, 3.0) for level in range(11)])
        self.assertLess(fitted, 0.3)
        self.assertLess(fitted, 0.5 * min(linear, concave))
        # The trial's own overshoot after level 2: 24.4 C against 21.0.
        self.assertAlmostEqual(max(r["t2ah"] for r in rows), 24.4, delta=0.05)

    def test_plant_follows_hac1s_measured_rule(self):
        plant = sim.Hac1Plant(level=3, setpoint=10, start=START)
        before = plant.level
        for _ in range(130):
            plant.step()
        self.assertEqual(plant.level, before - 2)  # one step per tick, two ticks
        plant.setpoint = 35.9
        for _ in range(61):
            plant.step()
        self.assertEqual(plant.level, before - 1)


class ClosedLoopTests(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.WARNING)
        self.addCleanup(logging.disable, logging.NOTSET)

    def run_loop(self, minutes, target=22.0, events=None, **plant):
        p = sim.Hac1Plant(start=START, **plant)
        pilot = AfterheatPilot()
        rows = sim.run(pilot, p, target=target, minutes=minutes, events=events)
        return pilot, p, rows

    def test_settles_within_half_a_kelvin_after_start(self):
        # Throttled coil (as measured) with the Calefa flow swinging +-2 K.
        for seed in (1, 2, 3, 4):
            with self.subTest(seed=seed):
                _pilot, _plant, rows = self.run_loop(30, gain=0.8, base=19.5, seed=seed,
                                                     tick_offset=7.3 * seed, water_amp=2)
                self.assertLessEqual(settle_minutes(rows, 22.0, end=30 * 60), 15.0)
                self.assertTrue(all(r["state"] == "pi" for r in rows))

    def test_water_flow_feed_forward_handles_the_full_calefa_swing(self):
        def with_flow(inp, plant, _gateway):
            inp.water_flow = plant.water()
        for seed in (1, 2, 3):
            with self.subTest(seed=seed):
                results = []
                for hook in (None, with_flow):
                    plant = sim.Hac1Plant(start=START, gain=0.8, base=19.5, seed=seed, tick_offset=7.3 * seed,
                                          water_amp=6)
                    rows = sim.run(AfterheatPilot(), plant, target=22.0, minutes=45, inputs_hook=hook)
                    late = [r["air"] - 22.0 for r in rows if r["t"] > 15 * 60]
                    results.append((settle_minutes(rows, 22.0), math.sqrt(sum(e * e for e in late) / len(late))))
                (_settle, rms_without), (settle, rms_with) = results
                self.assertLessEqual(settle, 15.0)
                self.assertLess(rms_with, rms_without)

    def test_settles_after_a_fan_step_from_27_to_54_percent(self):
        def fan_up(plant, _gateway):
            plant.fan = 54
        for seed in (1, 2, 3, 4):
            with self.subTest(seed=seed):
                _pilot, plant, rows = self.run_loop(
                    50, target=21.5, events={20 * 60: fan_up}, gain=0.6, curvature=6, base=20.0,
                    seed=seed, tick_offset=7.3 * seed, water_amp=2)
                self.assertEqual(plant.fan, 54)
                self.assertLessEqual(settle_minutes(rows, 21.5, end=20 * 60), 15.0)
                self.assertLessEqual(settle_minutes(rows, 21.5, start=20 * 60), 15.0)
                before = [r["level"] for r in rows if 15 * 60 <= r["t"] < 20 * 60]
                after = [r["level"] for r in rows if r["t"] >= 40 * 60]
                self.assertGreater(sum(after) / len(after), sum(before) / len(before) + 1)

    def test_no_limit_cycle_beyond_one_level_in_steady_state(self):
        # Concave coils as measured (the first levels give the most heat).
        for name, plant in (("throttled", dict(gain=0.8)), ("half open", dict(gain=0.6, curvature=6)),
                            ("opened", dict(gain=1.5))):
            for seed in (1, 2, 3):
                with self.subTest(plant=name, seed=seed):
                    _pilot, _plant, rows = self.run_loop(60, base=19.5, water_amp=0, seed=seed,
                                                         tick_offset=9.1 * seed, **plant)
                    steady = [r["level"] for r in rows if r["t"] >= 35 * 60]
                    self.assertLessEqual(max(steady) - min(steady), 2, (min(steady), max(steady)))

    def test_beats_hac1_alone(self):
        target = 22.0
        _pilot, _plant, rows = self.run_loop(60, gain=0.8, base=19.5, seed=5, water_amp=6)
        pi_rms = math.sqrt(sum((r["air"] - target) ** 2 for r in rows if r["t"] > 900)
                           / len([r for r in rows if r["t"] > 900]))
        native = sim.Hac1Plant(start=START, gain=0.8, base=19.5, seed=5, water_amp=6, setpoint=target)
        errors = []
        for second in range(3600):
            native.step()
            if second > 900:
                errors.append(native.x - target)
        native_rms = math.sqrt(sum(e * e for e in errors) / len(errors))
        self.assertLess(pi_rms, native_rms * 0.7)

    def test_setpoint_changes_stay_under_the_rate_limit(self):
        for seed in (1, 2, 3, 4, 5):
            with self.subTest(seed=seed):
                _pilot, _plant, rows = self.run_loop(45, gain=0.8, base=19.5, seed=seed,
                                                     tick_offset=7.3 * seed, water_amp=3)
                self.assertLessEqual(max(r["changes"] for r in rows), pilot_module.RATE_WARN_CHANGES)

    def test_recovers_from_a_glitch_step(self):
        def glitch_next_tick(plant, _gateway):
            plant.glitch_at_ticks.add(plant.tick_index + 1)
        pilot, plant, rows = self.run_loop(40, gain=0.8, base=19.5, seed=2, water_amp=0,
                                           events={25 * 60: glitch_next_tick})
        self.assertTrue(all(r["state"] == "pi" for r in rows))
        self.assertTrue(any("uventet" in r["reason"] for r in rows if r["t"] > 25 * 60))
        self.assertLessEqual(settle_minutes(rows, 22.0, start=30 * 60), 0.0)
        self.assertLess(pilot.follow_failures, pilot_module.FOLLOW_FAIL_LIMIT)

    def test_frost_floor_and_level_one(self):
        # Warm air before the coil: the controller would like level 0.
        p = sim.Hac1Plant(start=START, gain=0.8, base=19.0, level=2, setpoint=19)
        pilot = AfterheatPilot()
        sent = []

        def hook(inp, plant, _gateway):
            sent.append(pilot.setpoint)
        rows = sim.run(pilot, p, target=18.0, minutes=30, frost_lock=True, frost_floor=18.0, inputs_hook=hook)
        late = [r for r in rows if r["t"] > 10 * 60]
        self.assertTrue(all(r["level"] >= 1 for r in late))
        self.assertTrue(all(s is None or s >= 18 for s in sent))
        # Target 18 at the frost floor 18: the Pi steers for 19 (ceil(floor) + 1).
        raised = "Frostsikring: målet er hævet til 19,0 °C"
        self.assertTrue(any(r["reason"] == f"{raised} · mindst trin 1" for r in late))
        self.assertTrue(all(r["state"] == "pi" for r in rows))

    def test_saturation_at_level_10_is_not_a_fault(self):
        _pilot, plant, rows = self.run_loop(25, target=26.0, gain=0.8, base=19.5, water_amp=0)
        self.assertEqual(plant.level, 10)
        self.assertTrue(all(r["state"] == "pi" for r in rows))

    def test_falls_back_when_hac1_ignores_the_setpoint(self):
        def stuck(plant, _gateway):
            plant.ignore_setpoint = True
        pilot, _plant, rows = self.run_loop(25, target=23.5, gain=0.8, base=19.5, water_amp=0, level=1,
                                            events={60: stuck})
        fallback = [r for r in rows if r["state"] == "fallback"]
        self.assertTrue(fallback)
        self.assertIn("fulgte ikke", fallback[-1]["reason"])
        self.assertLessEqual(fallback[0]["t"], 8 * 60)
        self.assertIsNotNone(pilot.result["afterheat_fallback_remaining_seconds"])
        # In fallback the plain target is sent (native HAC1 control).
        self.assertEqual(rows[-1]["setpoint"], 24.0)


def level_changes_per_5_minutes(rows):
    changes = sum(1 for a, b in zip(rows, rows[1:]) if a["level"] != b["level"])
    return changes / ((rows[-1]["t"] - rows[0]["t"]) / 300)


def mean_error(rows, target):
    return sum(r["air"] - target for r in rows) / len(rows)


def longest_minutes(rows, condition):
    """Longest unbroken stretch (minutes) of rows meeting ``condition``."""
    longest = current = 0.0
    for a, b in zip(rows, rows[1:]):
        current = current + (b["t"] - a["t"]) if condition(b) else 0.0
        longest = max(longest, current)
    return longest / 60


def mean_abs_error(rows, target, start=10 * 60):
    late = [abs(r["air"] - target) for r in rows if r["t"] >= start]
    return sum(late) / len(late)


def fan_to(percent):
    def change(plant, _gateway):
        plant.fan = percent
    return change


class CalmControlTests(unittest.TestCase):
    """Opened valves (2026-11-20): one level is worth 1.5-2.4 K, the owner wants calm operation."""

    def setUp(self):
        logging.disable(logging.WARNING)
        self.addCleanup(logging.disable, logging.NOTSET)

    def run_loop(self, minutes, target=21.0, events=None, plant=None, **settings):
        p = sim.Hac1Plant(start=START, **{**sim.OPENED_VALVES, **(plant or {})})
        pilot = AfterheatPilot()
        rows = sim.run(pilot, p, target=target, minutes=minutes, events=events, **settings)
        return pilot, p, rows

    def test_opened_valves_give_one_and_a_half_to_two_and_a_half_kelvin_a_level(self):
        plant = sim.Hac1Plant(start=START, **sim.OPENED_VALVES, water_amp=0)
        for fan, low, high in ((27, 1.4, 1.7), (17, 2.2, 2.6)):
            plant.fan = fan
            self.assertTrue(low <= plant.rise(1) <= high, (fan, plant.rise(1)))
            self.assertAlmostEqual(plant.rise(3) - plant.rise(2), plant.rise(1), places=6)

    def test_steady_demand_is_calm(self):
        # Where a level settles near the target (21.1, 20.8) and where it needs two levels.
        for base in (19.3, 19.75, 18.35):
            for seed in (1, 2, 3):
                with self.subTest(base=base, seed=seed):
                    _pilot, _plant, rows = self.run_loop(60, plant=dict(base=base, seed=seed, tick_offset=7.3 * seed,
                                                                        water_amp=2))
                    self.assertTrue(all(r["state"] == "pi" for r in rows))
                    self.assertLessEqual(level_changes_per_5_minutes(rows), 0.5)
                    self.assertLessEqual(mean_abs_error(rows, 21.0), 0.5)
                    self.assertLessEqual(max(r["air"] for r in rows), 21.0 + 1.5)
                    self.assertLessEqual(max(r["changes"] for r in rows), pilot_module.RATE_LIMIT_CHANGES)

    def test_fan_steps_from_17_to_54_percent(self):
        # 27 % -> 17 % -> 54 % -> 27 %: 1.5 K, 2.4 K and 0.75 K a level.
        events = {20 * 60: fan_to(17), 45 * 60: fan_to(54), 70 * 60: fan_to(27)}
        rates, setpoint_rates = [], []
        for base in (19.1, 19.3, 19.7):
            for seed in (1, 2, 3):
                with self.subTest(base=base, seed=seed):
                    _pilot, plant, rows = self.run_loop(90, events=dict(events), plant=dict(
                        base=base, seed=seed, tick_offset=7.3 * seed, water_amp=2))
                    self.assertEqual(plant.fan, 27)
                    self.assertTrue(all(r["state"] == "pi" for r in rows))
                    rates.append(level_changes_per_5_minutes(rows))
                    self.assertLessEqual(rates[-1], 1.0)
                    self.assertLessEqual(mean_abs_error(rows, 21.0), 0.5)
                    after_start_or_step = [r["air"] for r in rows
                                           if any(0 <= r["t"] - at < 10 * 60 for at in (0, *events))]
                    self.assertLessEqual(max(after_start_or_step), 21.0 + 1.5)
                    setpoint_rates.append(sum(r["changes"] for r in rows) / len(rows))
                    self.assertLessEqual(max(r["changes"] for r in rows), pilot_module.RATE_WARN_CHANGES)
        self.assertLessEqual(sum(rates) / len(rates), 0.75)
        # Typically well below six setpoint changes in ten minutes (peaks come with the fan steps).
        self.assertLessEqual(sum(setpoint_rates) / len(setpoint_rates), 3.0)

    def test_air_settling_on_a_whole_degree_is_the_worst_case(self):
        # Level 1 settles on 21.0 C at 27 % fan and on ~22.0 C at 17 %: HAC1 cannot
        # hold air within a few hundredths of a whole degree, so it steps by itself
        # now and then and the Pi takes the step back. Bounded, never a fallback.
        events = {20 * 60: fan_to(17), 45 * 60: fan_to(54), 70 * 60: fan_to(27)}
        for seed in (1, 2, 3):
            with self.subTest(seed=seed):
                _pilot, _plant, rows = self.run_loop(90, events=dict(events), plant=dict(
                    base=19.5, seed=seed, tick_offset=7.3 * seed, water_amp=2))
                self.assertTrue(all(r["state"] == "pi" for r in rows))
                self.assertLessEqual(level_changes_per_5_minutes(rows), 1.25)
                self.assertLessEqual(mean_abs_error(rows, 21.0), 0.45)
                self.assertLessEqual(max(r["air"] for r in rows), 21.0 + 2.2)

    def test_heat_per_level_is_learned_within_two_level_changes(self):
        # 1.9 K a level at the reference (prior 1.5 K); a climb of two levels.
        for seed in (1, 2, 3):
            with self.subTest(seed=seed):
                pilot, _plant, rows = self.run_loop(8, target=21.5, plant=dict(
                    gain=1.6, base=18.0, level=0, water_amp=0, seed=seed, tick_offset=7.3 * seed))
                changes = [b["t"] for a, b in zip(rows, rows[1:]) if a["level"] != b["level"]]
                self.assertGreaterEqual(len(changes), 2)
                self.assertGreaterEqual(pilot.heat.measured, 2)
                for index in (0, 1):
                    self.assertAlmostEqual(pilot.heat.step(index), 1.6 * 1.2, delta=0.3)

    def test_wanted_level_stays_within_one_level_until_the_dead_time_has_passed(self):
        for seed in (1, 2):
            with self.subTest(seed=seed):
                _pilot, _plant, rows = self.run_loop(20, target=22.0, plant=dict(
                    base=17.5, level=0, water_amp=0, seed=seed, tick_offset=7.3 * seed))
                changed_at = None
                for a, b in zip(rows, rows[1:]):
                    if a["level"] != b["level"]:
                        changed_at = b["t"]
                    if changed_at is not None and b["t"] - changed_at < pilot_module.MODEL_DEAD_TIME_S:
                        self.assertLessEqual(b["wanted"], b["level"] + 1.05, b)

    def run_trial_valves(self, base, seed, minutes=60, **settings):
        p = sim.Hac1Plant(start=START, **{**sim.TRIAL_VALVES, "base": base, "level": 0,
                                          "water_amp": 2, "seed": seed, "tick_offset": 7.3 * seed})
        pilot = AfterheatPilot()
        return pilot, sim.run(pilot, p, target=21.0, minutes=minutes, **settings)

    def test_trial_valves_take_turns_when_no_level_fits(self):
        # The trial's coil: level 1 ~0.2 K and level 2 ~6 K. Level 1 leaves the
        # air 0.7-1.2 K cold, level 2 several K too warm: the Pi learns the big
        # step and then takes turns on the two (long-period PWM with the
        # minimum dwell) so that the air is right on average, instead of
        # sitting cold on level 1 for good.
        for base in (19.6, 20.1):
            for seed in (1, 2):
                with self.subTest(base=base, seed=seed):
                    pilot, rows = self.run_trial_valves(base, seed)
                    self.assertTrue(all(r["state"] == "pi" for r in rows))
                    self.assertGreater(pilot.heat.step(1), 2.5)
                    late = [r for r in rows if r["t"] >= 15 * 60]
                    self.assertTrue(any(r["reason"].startswith("Skifter mellem trin 1 og 2") for r in late))
                    self.assertLessEqual(abs(mean_error(late, 21.0)), 0.7)
                    self.assertLessEqual(max(r["level"] for r in late), 3)
                    self.assertLessEqual(level_changes_per_5_minutes(late), 1.05)
                    self.assertLessEqual(max(r["changes"] for r in late), pilot_module.RATE_WARN_CHANGES + 2)

    def test_trial_valves_never_sit_two_kelvin_cold(self):
        # Base 18.4 C: level 1 settles ~2.4 K cold and level 2 ~3.7 K warm. The
        # step gate alone held level 1 for the whole hour (mean error -2.4 K,
        # no level change); now the cold that lasts brings the turns.
        for seed in (1, 2, 3):
            with self.subTest(seed=seed):
                pilot, rows = self.run_trial_valves(18.4, seed)
                self.assertTrue(all(r["state"] == "pi" for r in rows))
                late = [r for r in rows if r["t"] >= 15 * 60]
                self.assertLessEqual(abs(mean_error(late, 21.0)), 0.8)
                self.assertGreaterEqual(sum(1 for a, b in zip(late, late[1:]) if a["level"] != b["level"]), 4)
                self.assertLessEqual(level_changes_per_5_minutes(late), 1.05)
                self.assertLessEqual(longest_minutes(rows, lambda r: r["air"] < 21.0 - 1.0), 20)
                self.assertLessEqual(max(r["level"] for r in late), 3)

    def test_throttled_coil_is_calm_too(self):
        for seed in (1, 2, 3):
            with self.subTest(seed=seed):
                p = sim.Hac1Plant(start=START, gain=0.8, base=19.5, seed=seed, tick_offset=7.3 * seed, water_amp=2)
                rows = sim.run(AfterheatPilot(), p, target=22.0, minutes=60)
                late = [r for r in rows if r["t"] >= 15 * 60]
                self.assertLessEqual(level_changes_per_5_minutes(late), 1.2)
                self.assertLessEqual(mean_abs_error(rows, 22.0, start=15 * 60), 0.35)


class PilotDecisionTests(unittest.TestCase):
    def inputs(self, now, **changes):
        values = dict(now=now, mode="pi", afterheat_on=True, target=21.5, level=3, level_age=1.0,
                      level_changed_at=None, t2ah=21.4, t2ah_age=1.0, t2ah_raw=21.42, fan_supply=27.0,
                      outdoor=5.0, extract=21.0, master=True)
        values.update(changes)
        return PilotInputs(**values)

    def test_blockers_hand_hac1_the_control(self):
        cases = {
            "mode": (dict(mode="native"), "native", "valgt i indstillinger"),
            "off": (dict(afterheat_on=False), "fallback", "slukket"),
            "override": (dict(override=True), "fallback", "forsøg"),
            "cool": (dict(cool_boost=True), "fallback", "Køl"),
            "free cooling": (dict(free_cooling=True), "fallback", "frikøling"),
            "standby": (dict(fans_stopped=True), "fallback", "anlægget er slukket"),
            "master": (dict(master=False), "fallback", "master"),
            "stale level": (dict(level_age=31.0), "fallback", "forældet"),
            "no level": (dict(level=None), "fallback", "forældet"),
            "stale air": (dict(t2ah_age=45.0), "fallback", "forældet"),
        }
        for name, (changes, state, text) in cases.items():
            with self.subTest(name):
                result = AfterheatPilot().update(self.inputs(START, **changes))
                self.assertEqual(result["afterheat_control_active"], state)
                self.assertIsNone(result["afterheat_hac1_setpoint"])
                self.assertIn(text, result["afterheat_control_reason"])

    def test_resumes_after_stale_data(self):
        pilot = AfterheatPilot()
        self.assertEqual(pilot.update(self.inputs(START))["afterheat_control_active"], "pi")
        self.assertEqual(pilot.update(self.inputs(START + 2, level_age=40.0))["afterheat_control_active"], "fallback")
        result = pilot.update(self.inputs(START + 4))
        self.assertEqual(result["afterheat_control_active"], "pi")
        self.assertIsNotNone(result["afterheat_hac1_setpoint"])

    def test_snapshot_keys(self):
        result = AfterheatPilot().update(self.inputs(START))
        for key in ("afterheat_control_mode", "afterheat_control_active", "afterheat_level",
                    "afterheat_level_wanted", "afterheat_level_target", "afterheat_level_max",
                    "afterheat_next_action", "afterheat_next_tick_seconds", "afterheat_target",
                    "afterheat_hac1_setpoint", "afterheat_control_error", "afterheat_control_reason"):
            self.assertIn(key, result)
        self.assertEqual(result["afterheat_level_max"], 10)
        self.assertEqual(result["afterheat_level"], 3)
        self.assertEqual(result["afterheat_control_error"], -0.1)
        self.assertIn(result["afterheat_next_action"], ("up", "hold", "down"))
        self.assertIsNone(result["afterheat_next_tick_seconds"])  # phase not known yet
        self.assertIsInstance(result["afterheat_hac1_setpoint"], int)

    def test_bumpless_start_holds_the_level_on_target(self):
        result = AfterheatPilot().update(self.inputs(START, t2ah=21.5, t2ah_raw=21.5))
        self.assertEqual(result["afterheat_level_target"], 3)
        self.assertEqual(result["afterheat_next_action"], "hold")
        self.assertEqual(result["afterheat_hac1_setpoint"], 21)
        self.assertTrue(result["afterheat_control_reason"].startswith("I komfortbåndet: holder trin 3"),
                        result["afterheat_control_reason"])
        self.assertIn("(mål 21,5 ±0,5)", result["afterheat_control_reason"])

    def test_frost_lock_level_zero_goes_up(self):
        result = AfterheatPilot().update(self.inputs(START, level=0, frost_lock=True, t2ah=25.0, t2ah_raw=25.0))
        self.assertEqual(result["afterheat_next_action"], "up")
        self.assertEqual(result["afterheat_control_reason"], "Frostsikring: mindst trin 1")
        self.assertGreaterEqual(result["afterheat_hac1_setpoint"], 18)

    def test_command_is_chosen_before_the_tick_and_frozen_around_it(self):
        pilot = AfterheatPilot()
        pilot.clock.observe(START + 1.5)  # a tick at START
        tick = START + pilot.clock.period
        pilot.update(self.inputs(tick - 40))
        self.assertIsNone(pilot.decided_for)
        self.assertEqual(pilot.result["afterheat_next_tick_seconds"], 40)
        pilot.update(self.inputs(tick - 14, t2ah=23.5, t2ah_raw=23.5))  # decision for this tick
        self.assertAlmostEqual(pilot.decided_for, tick)
        self.assertEqual(pilot.action, "down")
        decided = pilot.setpoint
        self.assertEqual(hac1_action(decided, 23.5), "down")
        for offset in (-8, -2, 3, 9):  # an up would be wanted now, but the tick is too close
            pilot.update(self.inputs(tick + offset, t2ah=19.0, t2ah_raw=19.0))
            self.assertEqual((pilot.action, pilot.setpoint), ("down", decided))
        pilot.update(self.inputs(tick + pilot.clock.period - 14, t2ah=19.0, t2ah_raw=19.0))
        self.assertEqual(pilot.action, "up")


    def test_a_held_tick_ends_the_run_of_misses(self):
        pilot = AfterheatPilot()
        pilot.update(self.inputs(START, t2ah=21.5, t2ah_raw=21.5))
        self.assertEqual(pilot.action, "hold")
        pilot.follow_failures = 2
        pilot.update(self.inputs(pilot.check_at + 1, t2ah=21.5, t2ah_raw=21.5))
        self.assertEqual(pilot.follow_failures, 0)

    def test_a_native_step_before_the_first_command_is_not_a_miss(self):
        pilot = AfterheatPilot()
        pilot.margin = 0.5  # left over from an earlier run
        pilot.clock.observe(START + 1.5)  # a tick at START
        tick = START + 3 * pilot.clock.period
        pilot.update(self.inputs(tick + 3))  # starts inside the freeze after a tick: no command yet
        self.assertIsNone(pilot.action)
        pilot.update(self.inputs(tick + 5, level=4, level_changed_at=tick + 4))
        self.assertEqual(pilot.follow_failures, 0)

    def test_a_long_glitch_in_the_raw_window_does_not_flip_the_command(self):
        # The raw window is full of a 3 K glitch; the filtered value is right.
        result = AfterheatPilot().update(self.inputs(START, t2ah=21.5, t2ah_raw=24.5))
        self.assertEqual(result["afterheat_next_action"], "hold")
        self.assertEqual(result["afterheat_hac1_setpoint"], 21)

    def test_comfort_band_holds_the_level(self):
        pilot = AfterheatPilot()
        pilot.update(self.inputs(START, target=21.0, level=0, t2ah=20.85, t2ah_raw=20.85))
        result = pilot.update(self.inputs(START + 2, target=21.0, level=0, t2ah=20.6, t2ah_raw=20.6))
        self.assertTrue(pilot.in_band)
        self.assertEqual(result["afterheat_next_action"], "hold")
        self.assertTrue(result["afterheat_in_band"])
        self.assertEqual(result["afterheat_control_reason"], "I komfortbåndet: holder trin 0 · luft 20,6 °C (mål 21,0 ±0,5)")
        # A wider band from the settings, in the reason too.
        result = pilot.update(self.inputs(START + 4, target=21.0, level=0, t2ah=20.6, t2ah_raw=20.6, band=0.8))
        self.assertIn("(mål 21,0 ±0,8)", result["afterheat_control_reason"])

    def test_band_hold_bleeds_the_integral_to_the_level(self):
        pilot = AfterheatPilot()
        pilot.update(self.inputs(START, t2ah=21.5, t2ah_raw=21.5))
        pilot.integral += 3.0  # as if it had wound up before the band
        for i in range(1, 900):  # 30 minutes: three times the bleed time
            pilot.update(self.inputs(START + 2 * i, t2ah=21.5, t2ah_raw=21.5))
        self.assertTrue(pilot.in_band)
        self.assertAlmostEqual(pilot.integral, pilot.heat.heat(3), delta=0.3)
        self.assertEqual(pilot.action, "hold")

    def test_a_step_hac1_takes_in_the_band_is_not_a_miss(self):
        pilot = AfterheatPilot()
        pilot.update(self.inputs(START, t2ah=21.5, t2ah_raw=21.5))
        self.assertTrue(pilot.band_hold)
        pilot.margin = 0.5
        pilot.update(self.inputs(START + 2, level=4, level_changed_at=START + 1, t2ah=21.5, t2ah_raw=21.5))
        self.assertEqual(pilot.follow_failures, 0)
        self.assertEqual(pilot.revert_level, 3)

    def after_a_step_down(self, air, seconds, **changes):
        """The real update() path: far too warm on level 2, HAC1 steps to 1, then ``air`` for ``seconds``.

        Returns the pilot and (seconds after the step, result) of every update.
        """
        pilot = AfterheatPilot()
        result = pilot.update(self.inputs(START, target=21.0, level=2, t2ah=23.3, t2ah_raw=23.3, **changes))
        self.assertEqual(result["afterheat_next_action"], "down")  # 2.3 K too warm: urgent
        seen = []
        for i in range(5, 5 + int(seconds) // 2):
            now = START + 2 * i
            result = pilot.update(self.inputs(now, target=21.0, level=1, level_changed_at=START + 10,
                                              t2ah=air, t2ah_raw=air, **changes))
            seen.append((now - START - 10, result))
        return pilot, seen

    def test_dwell_holds_a_level_before_a_step_that_is_not_urgent(self):
        # 0.9 K cold after the step down (outside the band, not urgent): the
        # level is kept for the dwell (3 ticks by default), then it goes up.
        pilot, seen = self.after_a_step_down(20.1, 4 * TICK)
        self.assertEqual(pilot.last_step_dir, -1)
        ups = [t for t, r in seen if r["afterheat_next_action"] == "up"]
        self.assertTrue(ups)
        self.assertGreaterEqual(ups[0], 3 * TICK - pilot_module.DECIDE_BEFORE_S - 10)
        self.assertLessEqual(ups[0], 4 * TICK)
        self.assertTrue(any(r["afterheat_control_reason"].startswith("Rolig drift: går et trin op")
                            for t, r in seen if t < ups[0]))
        # A shorter dwell from the settings: up at the first tick.
        _pilot, seen = self.after_a_step_down(20.1, 2 * TICK, min_dwell_ticks=1)
        ups = [t for t, r in seen if r["afterheat_next_action"] == "up"]
        self.assertTrue(ups and ups[0] <= TICK)

    def test_urgent_steps_override_the_dwell(self):
        # 1.2 K cold is beyond twice the band: _judge calls it urgent and the dwell is skipped.
        pilot, seen = self.after_a_step_down(19.8, 2 * TICK)
        ups = [t for t, r in seen if r["afterheat_next_action"] == "up"]
        self.assertTrue(ups and ups[0] <= TICK)
        self.assertTrue(pilot.urgent)

    def test_air_far_too_warm_is_urgent_and_skips_the_dwell(self):
        # Right after a step up: warm beyond twice the band (1 K at the default
        # 0.5 K band) is urgent already below 2 K, and above 2 K too: down at
        # the next tick either way (the dwell is skipped).
        band = pilot_module.PI_BAND_DEFAULT
        for air in (22.5, 23.4):
            with self.subTest(air=air):
                pilot = AfterheatPilot()
                pilot.update(self.inputs(START, target=21.0, level=1, t2ah=19.6, t2ah_raw=19.6))
                self.assertEqual(pilot.action, "up")
                downs = []
                for i in range(5, 5 + int(2 * TICK) // 2):
                    result = pilot.update(self.inputs(START + 2 * i, target=21.0, level=2,
                                                      level_changed_at=START + 10, t2ah=air, t2ah_raw=air))
                    if result["afterheat_next_action"] == "down":
                        downs.append(2 * i - 10)
                self.assertTrue(pilot.urgent)
                self.assertTrue(downs and downs[0] <= TICK)
        self.assertLess(2 * band, 22.5 - 21.0)
        self.assertLess(22.5 - 21.0, pilot_module.URGENT_WARM_K)

    def test_two_kelvin_warm_only_matters_for_a_step_that_overshoots(self):
        # Hot water, little air (a level is worth ~8 K): the air settled on level 2 in the band,
        # then warms. 1.5 K too warm is urgent (beyond twice the band),
        # but a step down would leave it further off (about -6.5 K): held. Over 2 K
        # too warm the step down is taken even though it overshoots.
        for air, action in ((22.5, "hold"), (23.4, "down")):
            with self.subTest(air=air):
                pilot = AfterheatPilot()
                for i in range(300):
                    pilot.update(self.inputs(START + 2 * i, target=21.0, level=2, t2ah=21.2, t2ah_raw=21.2,
                                             water_flow=81.0, fan_supply=15.0))
                self.assertEqual(pilot.action, "hold")
                result = pilot.update(self.inputs(START + 600, target=21.0, level=2, t2ah=air, t2ah_raw=air,
                                                  water_flow=81.0, fan_supply=15.0))
                self.assertTrue(pilot.urgent)
                after = pilot.error_settled - pilot.scale * pilot.heat.step(1)
                self.assertGreater(abs(after), abs(pilot.error_settled))  # the step down overshoots
                self.assertEqual(result["afterheat_next_action"], action)
                if action == "hold":
                    self.assertEqual(pilot.gate, "step")
                    self.assertIn("Holder trin 2: et trin ned flytter", result["afterheat_control_reason"])

    def test_air_below_the_frost_floor_is_urgent_never_in_the_band(self):
        # Target at the frost floor (18 C) and the widest band: the Pi steers
        # for 19 C (ceil(floor) + 1) and the band's cold edge stops at the
        # floor; 0.7 K below the floor is urgent, never in the band.
        pilot = AfterheatPilot()
        result = pilot.update(self.inputs(START, target=18.0, frost_lock=True, frost_floor=18.0, band=1.5,
                                          level=1, t2ah=17.3, t2ah_raw=17.3))
        self.assertFalse(result["afterheat_in_band"])
        self.assertTrue(pilot.urgent)
        self.assertEqual(result["afterheat_target"], 19.0)
        self.assertAlmostEqual(pilot.band[0], 1.0)  # the cold edge is the frost floor (19 - 1)
        self.assertEqual(result["afterheat_next_action"], "up")
        self.assertTrue(result["afterheat_control_reason"].startswith(
            "Frostsikring: målet er hævet til 19,0 °C · luften er under 18,0 °C"))
        self.assertNotIn("komfortbånd", result["afterheat_control_reason"])
        # Above the floor (target 19, floor 18) the cold edge stops at the floor.
        pilot = AfterheatPilot()
        pilot.update(self.inputs(START, target=19.0, frost_lock=True, frost_floor=18.0, band=1.5,
                                 level=1, t2ah=18.5, t2ah_raw=18.5))
        self.assertAlmostEqual(pilot.band[0], 1.0)

    def test_hac1_frost_steps_up_are_not_taken_back(self):
        # Floor 18.3 C: no setpoint below 19 may be sent, so HAC1 steps up by
        # itself with the air at 18.6 C. The Pi steers for 20 C (ceil(floor) + 1)
        # and asks for the step itself (level 1 settles below the floor HAC1 can
        # hold). When HAC1 steps up by itself during a hold (e.g. the model
        # still expected the air to rise) it is not a miss, not a blip, not undone.
        for band in (1.5, 0.1):
            with self.subTest(band=band):
                pilot = AfterheatPilot()
                frost = dict(target=18.3, frost_lock=True, frost_floor=18.3, band=band)
                for i in range(30):
                    result = pilot.update(self.inputs(START + 2 * i, level=1, t2ah=18.6, t2ah_raw=18.6, **frost))
                self.assertEqual(pilot.action, "up")
                self.assertTrue(pilot.frost_up)
                self.assertGreaterEqual(pilot.setpoint, 19)
                self.assertEqual(result["afterheat_target"], 20.0)
                self.assertTrue(result["afterheat_control_reason"].startswith(
                    "Frostsikring: målet er hævet til 20,0 °C"))
                pilot.action, pilot.commanded_dir = "hold", 0  # a hold asked for when HAC1 stepped
                for i in range(30, 60):
                    result = pilot.update(self.inputs(START + 2 * i, level=2, level_changed_at=START + 59,
                                                      t2ah=18.6, t2ah_raw=18.6, **frost))
                    self.assertIsNone(pilot.revert_level)
                    self.assertEqual(len(pilot.blips), 0)
                    self.assertEqual(pilot.follow_failures, 0)
                    self.assertNotEqual(result["afterheat_next_action"], "down")
                    self.assertEqual(result["afterheat_control_reason"],
                                     "Frostsikring: målet er hævet til 20,0 °C · HAC1 gik selv et trin op")

    def test_frost_lock_in_the_loop_keeps_the_air_off_the_floor(self):
        # Opened valves, target at the frost floor, level 1 settles ~0.2 K below
        # it and level 2 ~1.3 K above: level 2 is kept, level 1 only briefly.
        for seed in (1, 2):
            with self.subTest(seed=seed):
                p = sim.Hac1Plant(start=START, **{**sim.OPENED_VALVES, "base": 16.3, "seed": seed,
                                                  "tick_offset": 7.3 * seed, "water_amp": 2})
                pilot = AfterheatPilot()
                rows = sim.run(pilot, p, target=18.0, minutes=60, frost_lock=True, frost_floor=18.0, band=1.5)
                self.assertTrue(all(r["state"] == "pi" for r in rows))
                self.assertFalse([r for r in rows if r["air"] < 18.0 and "komfortbånd" in r["reason"]])
                late = [r for r in rows if r["t"] >= 10 * 60]
                self.assertGreaterEqual(mean_error(late, 18.0), 0.5)
                self.assertLessEqual(level_changes_per_5_minutes(late), 0.75)
                self.assertEqual(pilot.follow_failures, 0)

    def test_a_half_degree_frost_floor_does_not_pendle(self):
        # Floor 17.5 C (the UI allows half degrees): HAC1 cannot be sent a
        # setpoint below 18, so it steps up by itself below 18 C. Level 1
        # settles ~17.8 C (0.3 K above the floor) and level 2 ~19.3 C: a step
        # down to level 1 would only come back up (a down/up cycle that is
        # neither a blip nor a follow failure). Level 2 is kept; the Pi steers
        # for 19 C (ceil(floor) + 1), which level 2 is close to.
        for seed in (1, 2):
            with self.subTest(seed=seed):
                p = sim.Hac1Plant(start=START, **{**sim.OPENED_VALVES, "base": 16.3, "seed": seed,
                                                  "tick_offset": 7.3 * seed, "water_amp": 0,
                                                  "level": 2, "setpoint": 19})
                pilot = AfterheatPilot()
                rows = sim.run(pilot, p, target=17.5, minutes=60, frost_lock=True, frost_floor=17.5, band=0.5)
                self.assertTrue(all(r["state"] == "pi" for r in rows))
                late = [r for r in rows if r["t"] >= 10 * 60]
                self.assertEqual({r["level"] for r in late}, {2})
                self.assertFalse([r for r in rows if r["action"] == "down"])
                self.assertFalse([r for r in rows if r["reason"] == "Frostsikring: HAC1 gik selv et trin op"])
                self.assertTrue(late[-1]["reason"].startswith("Frostsikring: målet er hævet til 19,0 °C · "))
                self.assertLessEqual(abs(mean_error(late, 19.0)), 0.4)
                self.assertEqual(pilot.follow_failures, 0)

    # --- One frost-floor rule (_below_frost_floor) for every step down or neighbour.
    # Levels worth 0.9/0.9/1.2/1.5 K from level 0 with base 16 C: level 2 settles at
    # 17.8 C (below the frost floor HAC1 can hold), level 3 at 19.0 C (right on a
    # whole degree: no setpoint holds it), level 4 at 20.5 C.
    FROST_STEPS = [0.9, 0.9, 1.2, 1.5] + [1.5] * 6

    def frost_pilot(self, floor=18.0, **changes):
        values = dict(target=18.5, frost_lock=True, frost_floor=floor, band=0.5, level=3, t2ah=19.0, t2ah_raw=19.0)
        values.update(changes)
        pilot = AfterheatPilot()
        inp = self.inputs(START, **values)
        pilot.update(inp)
        pilot.heat.step = lambda index: self.FROST_STEPS[int(index)]
        pilot.scale = 1.0
        pilot.band = (0.5, 0.5)
        pilot.fan_moved = False
        pilot.last_step_at = None
        return pilot, inp

    def test_frost_floor_rules_out_the_lower_neighbour_of_an_unholdable_level(self):
        for floor in (18.0, 17.5):
            with self.subTest(floor=floor):
                pilot, inp = self.frost_pilot(floor)
                pilot.error_settled = 0.5  # level 3: 19.0 C, unholdable; level 2 would be 17.8 C
                self.assertEqual(pilot._away_from_unholdable(inp, 3, 1, 10), 0)
                self.assertFalse(pilot._acceptable(inp, -0.7))
                self.assertTrue(pilot._acceptable(inp, 0.5))
        pilot, _inp = self.frost_pilot()
        unlocked = self.inputs(START, target=18.5, band=0.5, level=3, t2ah=19.0, t2ah_raw=19.0)
        pilot.band = (0.8, 0.8)
        pilot.error_settled = 0.5
        self.assertEqual(pilot._away_from_unholdable(unlocked, 3, 0, 10), -1)  # without the lock it may

    def test_frost_floor_rules_out_a_lower_whole_degree_neighbour(self):
        # Target 18.3, floor 17.5: level 3 settles on 19.0 (in the band), level 2
        # at 18.1 is well inside the band and holdable, but below 18.2 (the floor
        # HAC1 can hold plus the margin): no step down to it.
        values = dict(target=18.3, frost_floor=17.5, band=1.5)
        pilot, inp = self.frost_pilot(**values)
        pilot.band = (0.8, 1.5)
        pilot.error_settled = 0.7
        pilot.heat.step = lambda index: {2: 0.9}.get(int(index), 3.0)
        self.assertEqual(pilot._off_whole_degree(inp, 3, 1, 10), 0)
        unlocked = self.inputs(START, target=18.3, band=1.5, level=3, t2ah=19.0, t2ah_raw=19.0)
        self.assertEqual(pilot._off_whole_degree(unlocked, 3, 1, 10), -1)

    def test_frost_lock_down_never_turns_into_up(self):
        # A step down wanted with the air at 18.88-18.99 C: the down setpoint
        # (17) is below the frost floor and no whole degree holds. The frost
        # floor's setpoint is sent (HAC1 holds or steps down), never 20 (up).
        for air in (18.88, 18.9, 18.93, 18.95, 18.97, 18.99, 19.1):
            with self.subTest(air=air):
                pilot, _inp = self.frost_pilot(level=4, t2ah=air, t2ah_raw=air)
                pilot.setpoint = None
                inp = self.inputs(START + 2, target=18.5, frost_lock=True, frost_floor=18.0, band=0.5,
                                  level=4, t2ah=air, t2ah_raw=air)
                action, sp, _note, _t, rule = pilot._choose_setpoint(inp, "down", 3.0, 1)
                self.assertEqual(sp, 18)
                self.assertIn(action, ("down", "hold"))
                self.assertEqual(rule, hac1_action(18, air))

    def test_frost_floor_step_down_is_forbidden_but_a_hold_beats_a_forced_up(self):
        # On level 3 (settles 19.0) with the air at 18.8 C rising: the step down
        # to level 2 (17.8 C, below the floor HAC1 can hold) is forbidden, yet
        # 18 holds at the air expected, so it is asked for, not 19 (a sure step up).
        pilot, inp = self.frost_pilot(t2ah=18.8, t2ah_raw=18.8)
        pilot.error_settled = 0.5
        pilot.setpoint = None
        self.assertEqual(pilot._step_cost(inp, 1, -1), pilot_module.STEP_COST_FORBIDDEN)
        self.assertEqual(pilot._hold_setpoint(inp, 1, (18.8, 18.85, 18.85), 18), 18)
        # Only when no setpoint holds (the air at 18.96 C: 18 steps down) is HAC1 forced up.
        self.assertEqual(pilot._hold_setpoint(inp, 1, (18.96, 18.97, 18.97), 18), 19)
        # A sure hold is kept even when it is the current setpoint's neighbour.
        pilot.setpoint, pilot.setpoint_level = 19, 3
        self.assertEqual(pilot._hold_setpoint(inp, 1, (18.8, 18.85, 18.85), 18), 18)
        # Right after a fan change the level's settled air counts as a guess:
        # on its way to 19.5 C, 19 holds, 18 would not.
        pilot.setpoint, pilot.fan_moved = None, True
        self.assertEqual(pilot._hold_setpoint(inp, 1, (18.8, 18.85, 18.85), 18), 19)
        # The frost minimum level itself stays forbidden.
        self.assertEqual(pilot._step_cost(self.inputs(START, target=18.5, frost_lock=True, frost_floor=18.0,
                                                      level=1, t2ah=19.0, t2ah_raw=19.0), 1, -1),
                         pilot_module.STEP_COST_FORBIDDEN)

    def test_frost_floor_revert_goes_back_up_and_never_down_to_it(self):
        # HAC1 stepped 3 -> 2 by itself and level 3 is marked unholdable: level 2
        # (17.8 C) is not "close enough to stay on" under the lock: back up.
        pilot, _inp = self.frost_pilot(level=2)
        inp = self.inputs(START + 2, target=18.5, frost_lock=True, frost_floor=18.0, band=0.5,
                          level=2, t2ah=18.9, t2ah_raw=18.9)
        pilot.error_settled, pilot.in_band = -0.7, False
        pilot.revert_level, pilot.revert_at = 3, START
        pilot.unholdable = {3: START + 1200}
        self.assertEqual(pilot._plan(inp, 2.0, 1, 10), ("up", 3))
        # HAC1 stepped 2 -> 3 by itself (target 18, level 2 settles 17.8): not taken back down.
        pilot, _inp = self.frost_pilot(target=18.0, level=3)
        inp = self.inputs(START + 2, target=18.0, frost_lock=True, frost_floor=18.0, band=0.5,
                          level=3, t2ah=18.5, t2ah_raw=18.5)
        pilot.error_settled, pilot.in_band = 1.0, False
        pilot.revert_level, pilot.revert_at = 2, START
        action, _level = pilot._plan(inp, 3.0, 1, 10)
        self.assertNotEqual(action, "down")
        self.assertIsNone(pilot.revert_level)

    def test_frost_lock_near_the_floor_holds_without_up_revert_loops(self):
        # The verifier's repro: target 18.5 with the frost floor at 18 / 17.5 C.
        # The Pi steers for 19 C (ceil(floor) + 1). Level 2 settles below the
        # floor HAC1 can hold, level 3 right on 19.0 C (no setpoint holds it:
        # HAC1 blips off it now and then by itself), level 4 at 20.5 C. A step
        # down to level 2 is forbidden, so the hold setpoint takes HAC1's blips
        # up to level 4 (taken back) rather than down below the floor.
        steps = [0.0, 0.9, 1.8, 3.0, 4.5, 6.0, 7.5, 9.0, 10.5, 12.0, 13.5]
        for floor in (18.0, 17.5):
            for seed in (1, 2, 3):
                with self.subTest(floor=floor, seed=seed):
                    p = sim.Hac1Plant(start=START, base=16.0, level_heat=steps, gain=1.0, water_mean=40.0,
                                      water_amp=0.0, seed=seed, tick_offset=7.3 * seed)
                    pilot = AfterheatPilot()
                    rows = sim.run(pilot, p, target=18.5, minutes=80, frost_lock=True, frost_floor=floor, band=0.5)
                    self.assertTrue(all(r["state"] == "pi" for r in rows))
                    late = [r for r in rows if r["t"] >= 10 * 60]
                    self.assertLessEqual(abs(mean_error(late, 19.0)), 0.4)
                    self.assertGreaterEqual(min(r["air"] for r in late), math.ceil(floor) - 0.2)
                    self.assertEqual(min(r["setpoint"] for r in rows), math.ceil(floor))
                    self.assertLessEqual(level_changes_per_5_minutes(late), 1.6)
                    late_pairs = list(zip(late, late[1:]))
                    # No 4 -> 5, never asked down to level 2, never more than a tick on it.
                    self.assertFalse([b["t"] for a, b in late_pairs if (a["level"], b["level"]) == (4, 5)])
                    self.assertFalse([b["t"] for a, b in late_pairs
                                      if (a["level"], b["level"]) == (3, 2) and a["action"] == "down"])
                    self.assertLessEqual(longest_minutes(late, lambda r: r["level"] <= 2), 1.1)
                    self.assertFalse([r for r in late if r["level"] >= 5])
                    self.assertEqual(pilot.follow_failures, 0)

    # --- 1.9.1: under the frost lock the target is raised to ceil(floor) + 1 K, a level
    # that settles below the floor HAC1 can hold is left at the next tick, and a step
    # down into one is never taken (HAC1 is held, or forced up only when nothing holds).
    def test_frost_lock_raises_the_target_to_a_degree_above_the_floor(self):
        for target, floor, raised in ((18.0, 18.0, 19.0), (18.5, 18.0, 19.0), (18.0, 17.5, 19.0),
                                      (18.3, 18.3, 20.0), (19.5, 18.0, None), (21.0, 18.0, None)):
            with self.subTest(target=target, floor=floor):
                pilot = AfterheatPilot()
                result = pilot.update(self.inputs(START, target=target, frost_lock=True, frost_floor=floor,
                                                  level=3, t2ah=19.4, t2ah_raw=19.4))
                reason = result["afterheat_control_reason"]
                if raised is None:
                    self.assertEqual(result["afterheat_target"], target)
                    self.assertNotIn("målet er hævet", reason)
                else:
                    self.assertEqual(result["afterheat_target"], raised)
                    self.assertAlmostEqual(pilot.control_target, pilot_module.hold_friendly(raised))
                    shown = f"{raised:.1f}".replace(".", ",")
                    self.assertTrue(reason.startswith(f"Frostsikring: målet er hævet til {shown} °C"))
                    self.assertEqual(reason.count("Frostsikring"), 1)
        # Without the lock the target is the user's, whatever the floor.
        result = AfterheatPilot().update(self.inputs(START, target=18.0, frost_floor=18.0, level=3,
                                                     t2ah=19.4, t2ah_raw=19.4))
        self.assertEqual(result["afterheat_target"], 18.0)
        self.assertNotIn("hævet", result["afterheat_control_reason"])
        # HAC1 on its own (a blocker): the user's target is shown.
        result = AfterheatPilot().update(self.inputs(START, mode="native", target=18.0, frost_lock=True,
                                                     frost_floor=18.0))
        self.assertEqual(result["afterheat_target"], 18.0)

    def test_frost_revert_goes_up_whatever_the_improve_margin(self):
        # Target 18, floor 17.5: HAC1 stepped 3 -> 2 by itself and level 2 settles
        # at 17.9 C, below the floor HAC1 can hold (18.2). Back up to level 3 (19.1)
        # leaves the air further from the target (1.1 > 0.1 K), yet it is taken.
        pilot, _inp = self.frost_pilot(target=18.0, floor=17.5, level=2)
        inp = self.inputs(START + 2, target=18.0, frost_lock=True, frost_floor=17.5, band=0.5,
                          level=2, t2ah=18.9, t2ah_raw=18.9)
        pilot.error_settled, pilot.in_band, pilot.frost_cold = -0.1, False, False
        pilot.revert_level, pilot.revert_at = 3, START
        self.assertGreater(abs(pilot._neighbour_error(2, 1)), abs(pilot.error_settled) + pilot_module.IMPROVE_MARGIN_K)
        self.assertEqual(pilot._plan(inp, 2.0, 1, 10), ("up", 3))
        self.assertTrue(pilot.frost_up)
        # Without HAC1's step (no revert pending) the level is left upwards all the same, past the step gate.
        pilot.revert_level = None
        self.assertEqual(pilot._plan(inp, 2.0, 1, 10), ("up", 3))
        self.assertIsNone(pilot.gate)
        # Settling clear of it (18.4 C) the level is kept as before.
        pilot.error_settled = 0.4
        self.assertNotEqual(pilot._plan(inp, 2.0, 1, 10)[0], "up")
        self.assertFalse(pilot.frost_up)

    def test_a_fresh_step_down_is_judged_as_the_step_into_it(self):
        # Just stepped 3 -> 2: the model has level 2 at 18.5 C, but a step down
        # into it (judged with the lasting step times the safety) would not have
        # been taken, so it is left before the step reaches the air.
        pilot, _inp = self.frost_pilot(level=2)
        pilot.heat.lasting_step = lambda index: 2.0
        inp = self.inputs(START + 2, target=18.5, frost_lock=True, frost_floor=18.0, band=0.5,
                          level=2, t2ah=19.5, t2ah_raw=19.5)
        pilot.error_settled, pilot.in_band, pilot.frost_cold = 0.0, False, False
        pilot.level_change_at, pilot.level_change_dir = START, -1
        self.assertTrue(pilot._down_below_frost_floor(inp, 3, pilot._neighbour_error(2, 1)))
        self.assertEqual(pilot._plan(inp, 2.0, 1, 10), ("up", 3))
        # Once the step has reached the air the model's own view stands.
        pilot.level_change_at = START - pilot_module.PWM_JUDGE_S
        self.assertNotEqual(pilot._plan(inp, 2.0, 1, 10), ("up", 3))

    def test_a_step_down_to_the_frost_minimum_level_loses_all_the_heat(self):
        # Level 2 settles at 21 C; the model has steps of 1.2 K, but the valve
        # barely opens on level 1 (the trial: 0.15 K, level 2 5 K more): the
        # step 2 -> 1 is judged as the whole heat of level 2 (2.4 K x 1.5).
        pilot, inp = self.frost_pilot(level=2, t2ah=21.0, t2ah_raw=21.0)
        pilot.heat.step = lambda index: 1.2
        pilot.heat.lasting_step = lambda index: 1.2
        pilot.error_settled = 2.5  # 21.0 C
        self.assertTrue(pilot._down_below_frost_floor(inp, 2))  # 21.0 - 3.6 = 17.4 < 18.2
        self.assertEqual(pilot._step_cost(inp, 1, -1), pilot_module.STEP_COST_FORBIDDEN)
        pilot.error_settled = 3.5  # 22.0 C: 18.4 stays clear
        self.assertFalse(pilot._down_below_frost_floor(inp, 2))
        # Higher up one step (with the safety) is what counts.
        self.assertFalse(pilot._down_below_frost_floor(inp, 4))  # 22.0 - 1.8 = 20.2
        self.assertFalse(pilot._down_below_frost_floor(self.inputs(START, target=18.5, level=2), 2))  # no lock

    def test_lasting_step_does_not_fade(self):
        heat = pilot_module.LevelHeat()
        heat.learn(1, 5.0, 0.8, START)
        self.assertGreater(heat.step(1), 4.0)
        for i in range(1, 60):
            heat.learn(4, 0.4, 0.8, START + i * 300)  # hours later, other steps only
        self.assertLess(heat.step(1), 1.0)  # the model's memory has faded back to the prior
        self.assertGreater(heat.lasting_step(1), 3.5)

    def test_pwm_does_not_keep_the_level_below_the_pair_under_the_frost_floor(self):
        # Taking turns on levels 3 and 4; HAC1 stepped one further down, to 2.
        pilot, _inp = self.frost_pilot(level=2)
        inp = self.inputs(START + 2, target=18.5, frost_lock=True, frost_floor=18.0, band=0.5,
                          level=2, t2ah=18.6, t2ah_raw=18.6)
        def pair():
            pilot.pwm_low, pilot.pwm_debt, pilot.pwm_settled = 3, 0.0, [None, None]
            pilot.pwm_up, pilot.pwm_turn_at, pilot.revert_level = False, None, None
        pair()
        pilot.error_settled = 0.3  # level 2 settles at 18.8 C: left alone, the cold turn
        self.assertEqual(pilot._pwm(inp, 2, 1, 10), ("hold", 2))
        pair()
        pilot.error_settled = -0.4  # 18.1 C, below the floor HAC1 can hold: not one of the pair
        self.assertIsNone(pilot._pwm(inp, 2, 1, 10))
        self.assertIsNone(pilot.pwm_low)

    def run_frost_repro(self, plant, target, floor):
        """Closed loop under the frost lock; also the Pi's own view of where the level settles."""
        pilot = AfterheatPilot()
        views = []

        def hook(inp, _plant, _gateway):  # the view of the update just before
            views.append((inp.now - START, int(inp.level), pilot.error_settled + AfterheatPilot.effective_target(inp)))
        rows = sim.run(pilot, plant, target=target, minutes=80, frost_lock=True, frost_floor=floor, band=0.5,
                       inputs_hook=hook)
        return pilot, rows, views

    def assert_off_the_frost_floor(self, rows, views, floor):
        late = [r for r in rows if r["t"] >= 10 * 60]
        below = sum(b["t"] - a["t"] for a, b in zip(late, late[1:]) if b["air"] < floor)
        self.assertLessEqual(below / 60, 1.0)
        self.assertGreaterEqual(min(r["air"] for r in late), floor - 0.3)
        # A level the Pi sees settle below the floor HAC1 can hold is never kept more than a tick.
        held_floor = math.ceil(floor) + pilot_module.FROST_MARGIN_K
        longest, since, level = 0.0, None, None
        for t, lvl, settled in views:
            if t < 10 * 60:
                continue
            if settled < held_floor and lvl < pilot_module.LEVEL_MAX:
                if since is None or lvl != level:
                    since, level = t, lvl
                longest = max(longest, t - since)
            else:
                since = level = None
        self.assertLessEqual(longest, 1.1 * TICK)

    def test_frost_repro_target_at_the_floor_keeps_the_air_off_it(self):
        # The verifier's repro (1.9.1): target 18 at a floor of 18 / 17.5 C; level
        # 2 settles at 17.8 C, level 3 at 19.0, level 4 at 20.5. HAC1 used to
        # step down to level 2 and stay there (the revert dropped by the improve
        # margin): the air fell below the floor.
        steps = [0.0, 0.9, 1.8, 3.0, 4.5, 6.0, 7.5, 9.0, 10.5, 12.0, 13.5]
        for floor in (18.0, 17.5):
            for seed in (1, 2, 3):
                with self.subTest(floor=floor, seed=seed):
                    p = sim.Hac1Plant(start=START, base=16.0, level_heat=steps, gain=1.0, water_mean=40.0,
                                      water_amp=0.0, seed=seed, tick_offset=7.3 * seed)
                    pilot, rows, views = self.run_frost_repro(p, 18.0, floor)
                    self.assertTrue(all(r["state"] == "pi" for r in rows))
                    self.assert_off_the_frost_floor(rows, views, floor)
                    self.assertEqual(pilot.follow_failures, 0)

    def test_frost_repro_trial_valves_keep_the_air_off_the_floor(self):
        # The trial's valves with cold air before the coil (12.5 C): level 1 adds
        # 0.15 K, level 2 5 K more (and swings with the Calefa flow). The model
        # learns that step short and forgets it; a tick on level 1 cost 2.5 K.
        p = sim.Hac1Plant(start=START, **{**sim.TRIAL_VALVES, "base": 12.5, "seed": 5, "tick_offset": 7.3 * 5})
        pilot, rows, views = self.run_frost_repro(p, 18.5, 18.0)
        self.assertTrue(all(r["state"] == "pi" for r in rows))
        self.assert_off_the_frost_floor(rows, views, 18.0)
        late = [r for r in rows if r["t"] >= 10 * 60]
        self.assertFalse([r for r in late if r["level"] <= 1])
        self.assertEqual(pilot.follow_failures, 0)

    def test_warm_air_into_the_coil_allows_low_levels_under_the_frost_lock(self):
        # Live 2026-11-21: air into the coil 18.4 C, floor 18; the Pi held level 2 at 25 C.
        pilot, inp = self.frost_pilot(level=2, t2ah=25.0, t2ah_raw=25.0)
        pilot.error_settled = 3.0
        pilot.heat.lasting_step = lambda index: 8.0
        warm = pilot_module.replace(inp, before_heater=18.6)
        self.assertFalse(pilot._down_below_frost_floor(warm, 2))
        self.assertFalse(pilot._below_frost_floor(warm, -10.0))
        cold = pilot_module.replace(inp, before_heater=16.0)
        self.assertTrue(pilot._down_below_frost_floor(cold, 2))
        # Level 0 (on/off) only with both the air in and the water side warm.
        self.assertEqual(AfterheatPilot.frost_min_level(pilot_module.replace(inp, before_heater=18.0, frost_sensor=17.0)), 0)
        self.assertEqual(AfterheatPilot.frost_min_level(pilot_module.replace(inp, before_heater=18.0, frost_sensor=9.0)), 1)
        self.assertEqual(AfterheatPilot.frost_min_level(pilot_module.replace(inp, before_heater=None, frost_sensor=17.0)), 1)

    def test_a_level_seen_well_above_the_floor_may_be_stepped_down_to(self):
        # Live 2026-11-21: level 1 settled at ~20.5 C with the floor 18, yet the
        # model rule (all heat above level 0 lost) kept the Pi on level 2.
        pilot, inp = self.frost_pilot(level=2, t2ah=21.8, t2ah_raw=21.8)
        pilot.error_settled = 1.0
        pilot.scale = 1.0
        pilot.level_seen[1] = (inp.now - 60, 20.5, 1.0)
        self.assertFalse(pilot._down_below_frost_floor(inp, 2))
        # Seen close to the floor, or with more air now: not allowed by it.
        pilot.level_seen[1] = (inp.now - 60, 18.4, 1.0)
        self.assertTrue(pilot._down_below_frost_floor(inp, 2))
        pilot.level_seen[1] = (inp.now - 60, 20.5, 1.5)
        pilot.heat.lasting_step = lambda index: 3.0
        self.assertTrue(pilot._down_below_frost_floor(inp, 2))

    def test_frost_step_down_is_forbidden_only_where_a_blip_could_reach_the_floor(self):
        # Level 4 settles at 19.7 C, level 3 by the model (1.5 K x 1.5) too close
        # to the floor HAC1 can hold (18.2 C). HAC1 stepping down by itself for a
        # tick is taken back at the next one: with the air at 20.0 C it loses
        # about half the step (20.0 - 0.75 x 1.5 = 18.9): a normal step, priced
        # as such. With the air at 19.2 C it could reach the floor: forbidden.
        pilot, inp = self.frost_pilot(level=4, t2ah=20.0, t2ah_raw=20.0)
        pilot.heat.lasting_step = lambda index: 0.0
        pilot.error_settled = 1.2
        self.assertTrue(pilot._down_below_frost_floor(inp, 4))
        self.assertAlmostEqual(pilot._step_cost(inp, 1, -1), 0.3)
        near = self.inputs(START, target=18.5, frost_lock=True, frost_floor=18.0, band=0.5, level=4,
                           t2ah=19.2, t2ah_raw=19.2)
        self.assertEqual(pilot._step_cost(near, 1, -1), pilot_module.STEP_COST_FORBIDDEN)
        # A step down to the frost minimum level stays forbidden however warm the air is.
        far = self.inputs(START, target=18.5, frost_lock=True, frost_floor=18.0, band=0.5, level=2,
                          t2ah=25.0, t2ah_raw=25.0)
        self.assertEqual(pilot._step_cost(far, 1, -1), pilot_module.STEP_COST_FORBIDDEN)

    def test_frost_lock_level_just_above_a_whole_degree_does_not_cycle(self):
        # The verifier's repro (1.9.1): the trial's valves, 12.5 C before the
        # coil, steady water. Level 3 settles ~0.7 K above 19 C. After HAC1's
        # blip up to level 4 was taken back, the air from it still read above
        # 19.95 C, the step down was priced forbidden and 20 was asked for; as
        # the air settled HAC1 stepped up by itself again: a 3-minute 3 <-> 4
        # cycle (~20 own steps an hour).
        for seed in range(1, 9):
            with self.subTest(seed=seed):
                p = sim.Hac1Plant(start=START, seed=seed, tick_offset=7.3 * seed,
                                  **{**sim.TRIAL_VALVES, "base": 12.5, "water_mean": 44.0, "water_amp": 0.0})
                pilot = AfterheatPilot()
                rows = sim.run(pilot, p, target=19.0, minutes=80, frost_lock=True, frost_floor=18.0)
                self.assertTrue(all(r["state"] == "pi" for r in rows))
                late = [r for r in rows if r["t"] >= 20 * 60]
                own = [b["t"] for a, b in zip(late, late[1:]) if b["level"] != a["level"] and a["action"] == "hold"]
                self.assertLessEqual(len(own), 2)
                self.assertGreaterEqual(min(r["air"] for r in late), 18.0)

    def test_frost_lock_steps_down_one_at_a_time_however_warm(self):
        # Over URGENT_WARM_K too warm, and right after a fan change, a step down
        # still waits for the last one to show under the frost lock (one learned
        # short would only show its cold after the next one was taken too).
        for fan_moved in (False, True):
            with self.subTest(fan_moved=fan_moved):
                pilot, inp = self.frost_pilot(level=3, t2ah=21.0, t2ah_raw=21.0)
                pilot.heat.lasting_step = lambda index: 0.0
                pilot.error_settled, pilot.error_now, pilot.urgent = 3.0, 2.5, True
                pilot.in_band, pilot.sigma, pilot.fan_moved = False, 0.0, fan_moved
                pilot.last_step_dir, pilot.last_step_at = -1, START - 45
                pilot.level_change_at = None
                self.assertEqual(pilot._plan(inp, 2.0, 1, 10), ("hold", 3))
                self.assertEqual(pilot.gate, "wait")
                # Without the lock the urgent step is taken at once, as before.
                free = self.inputs(START, target=18.5, band=0.5, level=3, t2ah=21.0, t2ah_raw=21.0)
                self.assertEqual(pilot._plan(free, 2.0, 0, 10), ("down", 2))

    def test_frost_lock_fan_change_with_a_short_first_opening_stays_off_the_floor(self):
        # Random sweep case 483: a valve that opens late (level 3 adds 4 K), warm
        # water swinging, the fan stepping. Too warm right after a fan change,
        # the Pi used to command two steps down a tick apart before either
        # showed and the air fell ~1 K below the frost floor.
        steps = [0, 0.2, 0.4, 4.5, 5.0, 5.4, 5.8, 6.1, 6.4, 6.6, 6.8]
        fans = {20: 20.0, 32: 35.0, 44: 17.0, 56: 30.0, 68: 22.0}
        p = sim.Hac1Plant(start=START, seed=6, tick_offset=7.3 * 6, base=14.0, gain=1.0, level_heat=steps,
                          water_mean=50.0, water_amp=6.0, water_period=2400.0)
        events = {m * 60: (lambda f: (lambda plant, _gateway: setattr(plant, "fan", f)))(f) for m, f in fans.items()}
        rows = sim.run(AfterheatPilot(), p, target=21.0, minutes=80, frost_lock=True, frost_floor=18.0, events=events)
        late = [r for r in rows if r["t"] >= 15 * 60]
        self.assertGreaterEqual(min(r["air"] for r in late), 18.0 - 0.3)
        self.assertTrue(all(r["state"] == "pi" for r in rows))

    def test_a_held_step_does_not_wind_up_the_integral(self):
        # 0.6 K cold on level 0 and a level worth 1.5 K: the step gate holds
        # the level for 15 minutes. The integral does not wind up against it
        # and the quantizer's sum stays below a level (no stale demand).
        pilot = AfterheatPilot()
        wanted = []
        for i in range(450):
            result = pilot.update(self.inputs(START + 2 * i, target=21.0, level=0, t2ah=20.4, t2ah_raw=20.4))
            wanted.append(result["afterheat_level_wanted"])
        self.assertEqual(result["afterheat_next_action"], "hold")
        self.assertLess(max(wanted), 1.0)
        self.assertLess(pilot.heat.level(pilot.integral), 0.5)
        self.assertLess(abs(pilot.sigma), 0.5 + pilot_module.QUANTIZER_HYSTERESIS)

    def test_a_step_that_would_overshoot_more_is_not_taken(self):
        # 0.6 K cold at level 0 and a level worth 1.5 K: +0.9 K would be worse.
        pilot = AfterheatPilot()
        for i in range(120):  # four minutes: the integral asks for a level
            result = pilot.update(self.inputs(START + 2 * i, target=21.0, level=0, t2ah=20.4, t2ah_raw=20.4))
        self.assertGreater(result["afterheat_level_wanted"], 0.0)
        self.assertEqual(result["afterheat_next_action"], "hold")
        self.assertEqual(pilot.gate, "step")
        self.assertIn("Holder trin 0: et trin op flytter ca. 1,5 K", result["afterheat_control_reason"])

    def test_hold_at_level_zero_keeps_its_setpoint_as_the_air_drifts(self):
        pilot = AfterheatPilot()
        sent = set()
        for i, air in enumerate((21.3, 21.1, 20.9, 20.75, 20.9, 21.2, 21.4)):
            pilot.update(self.inputs(START + 2 * i, target=21.0, level=0, t2ah=air, t2ah_raw=air))
            sent.add(pilot.setpoint)
            self.assertEqual(hac1_action(pilot.setpoint, air), "down")  # nothing below 0: a hold
        self.assertEqual(len(sent), 1)

    def test_rate_limit_never_keeps_a_setpoint_below_the_frost_floor(self):
        pilot = AfterheatPilot()
        pilot.update(self.inputs(START, target=17.5, t2ah=17.4, t2ah_raw=17.4))
        pilot.setpoint = 17
        pilot.changes.extend(START + i for i in range(pilot_module.RATE_LIMIT_CHANGES))
        pilot.update(self.inputs(START + 2, target=18.0, frost_lock=True, t2ah=17.4, t2ah_raw=17.4))
        self.assertGreaterEqual(pilot.setpoint, 18)

class EngineTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        patcher = mock.patch.object(controller_core, "AFTERHEAT_OVERRIDE_PATH", self.dir / "override.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.state = ControllerState(self.dir / "controller.json")
        self.state.configure({"afterheat_coil": "water", "afterheat_setpoint": 21})
        self.writes = []
        hardware = HardwareAdapter(set_afterheat_setpoint=self.writes.append, set_fireplace=lambda *_: None,
                                   set_standby=lambda *_: None, write_fan_pair=lambda *_: None)
        self.engine = ControllerEngine(self.state, hardware)
        self.engine.retry_base_seconds = 0.0
        self.engine.set_external({"outdoor_temperature": 8.0, "attic_temperature": 9.0,
                                  "afterheat_frost_temperature": 20.0})

    def feed(self, now, level=3, t2ah=20.4, master=True, age=1.0):
        self.engine.set_afterheat_inputs({
            "level": level, "level_at": now - age, "level_changed_at": None,
            "t2ah": t2ah, "t2ah_at": now - age, "t2ah_raw": t2ah + 0.02, "fan_supply": 27, "master": master})

    def test_calm_settings_are_validated_and_reach_the_pilot(self):
        self.assertEqual(self.state.data["afterheat_pi_band"], 0.5)
        self.assertEqual(self.state.data["afterheat_pi_min_dwell_ticks"], 3)
        for bad in ({"afterheat_pi_band": 0.05}, {"afterheat_pi_band": 2.0},
                    {"afterheat_pi_min_dwell_ticks": 0}, {"afterheat_pi_min_dwell_ticks": 11},
                    # Not cut to 2 (or to 1 or 0): a fraction, a flag or an empty field is refused.
                    {"afterheat_pi_min_dwell_ticks": 2.5}, {"afterheat_pi_min_dwell_ticks": "2.5"},
                    {"afterheat_pi_min_dwell_ticks": True}, {"afterheat_pi_min_dwell_ticks": ""},
                    {"afterheat_pi_min_dwell_ticks": None}):
            with self.subTest(**bad), self.assertRaises(ControllerError):
                self.state.configure(bad)
        self.assertEqual(self.state.data["afterheat_pi_min_dwell_ticks"], 3)
        self.state.configure({"afterheat_pi_min_dwell_ticks": 4.0})  # a whole number sent as a float is fine
        self.assertEqual(self.state.data["afterheat_pi_min_dwell_ticks"], 4)
        self.assertIsInstance(self.state.data["afterheat_pi_min_dwell_ticks"], int)
        self.state.configure({"afterheat_control_mode": "pi", "afterheat_pi_band": 0.8,
                              "afterheat_pi_min_dwell_ticks": 5})
        snapshot = self.state.snapshot()
        self.assertEqual(snapshot["afterheat_pi_band"], 0.8)
        self.assertEqual(snapshot["afterheat_pi_min_dwell_ticks"], 5)
        seen = []
        original = self.engine.afterheat_pilot.update

        def spy(inp):
            seen.append((inp.band, inp.min_dwell_ticks))
            return original(inp)
        self.engine.afterheat_pilot.update = spy
        now = time.time()
        self.feed(now)
        self.engine.resolve(now)
        self.assertEqual(seen[-1], (0.8, 5))
        self.assertEqual(ControllerState(self.dir / "controller.json").data["afterheat_pi_band"], 0.8)

    def test_mode_setting_is_validated_and_defaults_to_native(self):
        self.assertEqual(self.state.data["afterheat_control_mode"], "native")
        with self.assertRaises(ControllerError):
            self.state.configure({"afterheat_control_mode": "auto"})
        with self.assertRaises(ControllerError):
            self.state.configure({"afterheat_pi_gain": 9})
        self.state.configure({"afterheat_control_mode": "pi", "afterheat_pi_gain": 1.2})
        self.assertEqual(ControllerState(self.dir / "controller.json").data["afterheat_control_mode"], "pi")

    def test_native_mode_is_todays_behaviour(self):
        now = time.time()
        self.feed(now)
        result = self.engine.resolve(now)
        self.assertEqual(result["afterheat_command"], 21)
        self.assertEqual(result["afterheat_control_active"], "native")
        self.assertEqual(result["afterheat_level"], 3)
        self.assertEqual(result["afterheat_target"], 21.0)

    def test_pi_mode_sends_the_step_command(self):
        self.state.configure({"afterheat_control_mode": "pi", "afterheat_setpoint": 21.5})
        now = time.time()
        self.feed(now)
        result = self.engine.resolve(now)
        self.assertEqual(result["afterheat_control_active"], "pi")
        self.assertEqual(result["afterheat_target"], 21.5)
        self.assertEqual(result["afterheat_next_action"], "up")
        self.assertEqual(result["afterheat_command"], result["afterheat_hac1_setpoint"])
        self.assertGreater(result["afterheat_command"], 20.4)
        self.engine.apply()
        self.assertIsInstance(self.writes[-1], int)

    def test_fractional_setpoint_is_rounded_natively(self):
        self.state.configure({"afterheat_setpoint": 21.5})
        self.assertEqual(self.state.data["afterheat_setpoint"], 21.5)
        self.assertEqual(self.engine.resolve()["afterheat_command"], 22)
        self.state.configure({"afterheat_setpoint": 21})
        self.assertIsInstance(self.state.data["afterheat_setpoint"], int)

    def test_stale_or_not_master_falls_back_to_the_plain_target(self):
        self.state.configure({"afterheat_control_mode": "pi"})
        now = time.time()
        for kwargs in (dict(age=40.0), dict(master=False)):
            with self.subTest(**kwargs):
                self.feed(now, **kwargs)
                result = self.engine.resolve(now)
                self.assertEqual(result["afterheat_control_active"], "fallback")
                self.assertEqual(result["afterheat_command"], 21)

    def test_override_file_wins(self):
        self.state.configure({"afterheat_control_mode": "pi"})
        controller_core.AFTERHEAT_OVERRIDE_PATH.write_text(
            json.dumps({"setpoint": 23.25, "expires_at": time.time() + 60}))
        now = time.time()
        self.feed(now)
        result = self.engine.resolve(now)
        self.assertEqual(result["afterheat_command"], 23.25)
        self.assertEqual(result["afterheat_control_active"], "fallback")

    def test_cool_boost_keeps_10_c(self):
        self.state.configure({"afterheat_control_mode": "pi", "cool_boost_minutes": 30})
        now = time.time()
        self.feed(now)
        result = self.engine.resolve(now)
        self.assertEqual(result["afterheat_command"], 10)
        self.assertEqual(result["afterheat_control_active"], "fallback")

    def test_frost_floor_is_the_last_word(self):
        self.engine.frost_engage_seconds = 0
        self.engine.set_external({"outdoor_temperature": -3.0, "attic_temperature": -1.0,
                                  "afterheat_frost_temperature": 20.0})
        self.state.configure({"afterheat_control_mode": "pi"})
        now = time.time()
        self.feed(now, level=4, t2ah=26.0)  # far too warm: the Pi wants to go down
        result = self.engine.resolve(now)
        self.assertTrue(result["afterheat_frost_lock"])
        self.assertEqual(result["afterheat_target"], 21.0)
        self.assertEqual(result["afterheat_control_active"], "pi")
        self.assertGreaterEqual(result["afterheat_command"], 18)

    def test_standby_switches_off(self):
        self.state.configure({"afterheat_control_mode": "pi", "standby_minutes": 60})
        now = time.time()
        self.feed(now)
        result = self.engine.resolve(now)
        self.assertIsNone(result["afterheat_command"])
        self.assertEqual(result["afterheat_control_active"], "fallback")

    def test_native_setpoint_for_the_gateway_stop(self):
        self.state.configure({"afterheat_control_mode": "pi", "afterheat_setpoint": 21.6})
        now = time.time()
        self.feed(now)
        self.engine.resolve(now)
        self.assertEqual(self.engine.afterheat_native_setpoint(), 22)
        self.state.configure({"afterheat_enabled": False})
        self.engine.resolve(now)
        self.assertIsNone(self.engine.afterheat_native_setpoint())


class RuntimeTests(unittest.TestCase):
    def test_runtime_feeds_the_pilot_and_the_history(self):
        from controller_runtime import ControllerRuntime
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        mono = time.monotonic()
        state = {
            "hac1_output_level": 4, "hac1_output_level_monotonic": mono - 1,
            "hac1_output_level_changed_monotonic": mono - 20,
            "heating_coil_after_temperature": 21.3, "heating_coil_after_temperature_sample_monotonic": mono - 2,
            "t2ah_last_raw_temperature": 21.35, "t2ah_last_raw_sample_monotonic": mono - 2,
            "fan_supply_percent": 36,
        }
        runtime = ControllerRuntime(gateway_state=state, hardware=HardwareAdapter(),
                                    state_path=Path(temp.name) / "controller.json")
        inputs = runtime._afterheat_inputs()
        self.assertEqual(inputs["level"], 4)
        self.assertAlmostEqual(time.time() - inputs["level_at"], 1, delta=0.5)
        self.assertAlmostEqual(time.time() - inputs["level_changed_at"], 20, delta=0.5)
        self.assertEqual(inputs["t2ah"], 21.3)
        self.assertAlmostEqual(inputs["t2ah_raw"], 21.35)
        self.assertEqual(inputs["fan_supply"], 36)
        self.assertIs(inputs["master"], False)
        snapshot = runtime.snapshot()
        self.assertEqual(snapshot["afterheat_level"], 4)
        self.assertEqual(snapshot["afterheat_control_active"], "native")
        runtime.tick()
        self.assertEqual(state["afterheat_level"], 4)
        self.assertIn("afterheat_target", state)

    def test_gateway_stop_leaves_hac1_the_plain_target(self):
        import importlib.machinery, types
        if "paho.mqtt.client" not in sys.modules:
            paho = types.ModuleType("paho"); mp = types.ModuleType("paho.mqtt"); mc = types.ModuleType("paho.mqtt.client")
            mc.__spec__ = importlib.machinery.ModuleSpec("paho.mqtt.client", loader=None)
            paho.mqtt = mp; mp.client = mc
            sys.modules.update({"paho": paho, "paho.mqtt": mp, "paho.mqtt.client": mc})
        try:
            import dantherm_gateway
        except ModuleNotFoundError as error:
            self.skipTest(str(error))
        gateway = dantherm_gateway.Gateway.__new__(dantherm_gateway.Gateway)
        written = []
        gateway.write_afterheat_setpoint = lambda ser, value: written.append(value)
        engine = types.SimpleNamespace(afterheat_native_setpoint=lambda: 22, last_applied={"afterheat_setpoint": 24})
        master = types.SimpleNamespace(writes_allowed=lambda: True)
        gateway.controller = types.SimpleNamespace(engine=engine, master=master)
        gateway.restore_afterheat_on_exit(None)
        self.assertEqual(written, [22])
        engine.last_applied["afterheat_setpoint"] = 22  # already the plain target
        gateway.restore_afterheat_on_exit(None)
        master.writes_allowed = lambda: False  # HCP4 is master: hands off
        engine.last_applied["afterheat_setpoint"] = 24
        gateway.restore_afterheat_on_exit(None)
        self.assertEqual(written, [22])

    def test_history_fields(self):
        import dashboard_server
        for field in ("afterheat_level", "afterheat_level_target", "afterheat_target"):
            self.assertIn(field, dashboard_server.HISTORY_FIELDS)


if __name__ == "__main__":
    unittest.main()
