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
        self.assertTrue(any(r["reason"] == "Frostsikring: mindst trin 1" for r in late))
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
        self.assertTrue(result["afterheat_control_reason"].startswith("Holder trin 3"))

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
