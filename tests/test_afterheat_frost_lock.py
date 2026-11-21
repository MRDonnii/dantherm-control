"""Afterheat frost lock: a water coil in a cold attic must never be switched off while it freezes."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "gateway"))

from controller_core import (
    FROST_LOCK_RELEASE_SECONDS,
    ControllerEngine,
    ControllerError,
    ControllerState,
    HardwareAdapter,
)
from webui_permissions import config_allowed

OFF_ERROR = "Frostsikring: eftervarmen kan ikke slukkes, mens det fryser."
FLOOR_ERROR = "Frostsikring: eftervarmen kan ikke sættes under 18 °C, mens det fryser."
COOL_ERROR = "Frostsikring: Køl kan ikke startes, mens det fryser."


class FrostLockEngineTests(unittest.TestCase):
    def make_engine(self, *, coil="water", writes=None, **hardware):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state_path = Path(temp.name) / "controller.json"
        state = ControllerState(self.state_path)
        state.configure({"afterheat_coil": coil})
        if writes is not None:
            hardware.setdefault("set_afterheat_setpoint", writes.append)
        for name in ("set_fireplace", "set_standby", "write_fan_pair"):
            hardware.setdefault(name, lambda *_args: None)
        engine = ControllerEngine(state, HardwareAdapter(**hardware))
        engine.retry_base_seconds = 0.0
        engine.frost_engage_seconds = 0  # most tests check the latched behaviour
        return engine

    @staticmethod
    def readings(engine, outdoor=None, attic=None, frost=None):
        engine.set_external({"outdoor_temperature": outdoor, "attic_temperature": attic,
                             "afterheat_frost_temperature": frost})

    def test_defaults_and_settings_are_in_the_snapshot(self):
        engine = self.make_engine()
        result = engine.resolve()
        self.assertIs(result["frost_lock_enabled"], True)
        self.assertEqual(result["frost_lock_outdoor_c"], 3.0)
        self.assertEqual(result["frost_lock_coil_c"], 8.0)
        self.assertEqual(result["frost_lock_min_setpoint_c"], 18.0)
        self.assertIs(result["afterheat_frost_lock"], False)
        self.assertIsNone(result["afterheat_frost_lock_reason"])
        self.assertEqual(result["afterheat_frost_lock_floor"], 18.0)

    def test_cold_outdoor_locks_a_water_coil_with_reason(self):
        engine = self.make_engine()
        self.readings(engine, outdoor=-1.6)
        result = engine.resolve()
        self.assertIs(result["afterheat_frost_lock"], True)
        self.assertEqual(result["afterheat_frost_lock_reason"], "Frost: ude -1,6 °C (grænse 3 °C)")

    def test_attic_and_frost_sensor_lock_too(self):
        for attic, frost, text in ((2.5, None, "loft 2,5 °C"), (None, 7.9, "frostføler 7,9 °C (grænse 8 °C)")):
            with self.subTest(attic=attic, frost=frost):
                engine = self.make_engine()
                self.readings(engine, outdoor=12.0, attic=attic, frost=frost)
                result = engine.resolve()
                self.assertIs(result["afterheat_frost_lock"], True)
                self.assertIn(text, result["afterheat_frost_lock_reason"])

    def test_a_lone_cold_reading_does_not_latch(self):
        engine = self.make_engine()
        engine.frost_engage_seconds = 120
        now = 1_000_000.0
        self.readings(engine, outdoor=0.0, attic=20.0, frost=22.0)
        self.assertFalse(engine.resolve(now)["afterheat_frost_lock"])
        self.readings(engine, outdoor=16.0, attic=20.0, frost=22.0)
        self.assertFalse(engine.resolve(now + 30)["afterheat_frost_lock"])
        self.readings(engine, outdoor=-2.0, attic=20.0, frost=22.0)
        self.assertFalse(engine.resolve(now + 60)["afterheat_frost_lock"])
        self.assertFalse(engine.resolve(now + 179)["afterheat_frost_lock"])
        self.assertTrue(engine.resolve(now + 180)["afterheat_frost_lock"])

    def test_electric_coil_or_disabled_lock_does_nothing(self):
        engine = self.make_engine(coil="electric")
        self.readings(engine, outdoor=-5)
        self.assertIs(engine.resolve()["afterheat_frost_lock"], False)
        engine = self.make_engine()
        engine.config.configure({"frost_lock_enabled": False})
        self.readings(engine, outdoor=-5)
        self.assertIs(engine.resolve()["afterheat_frost_lock"], False)
        # Switching it on again acts at once: the cold was tracked all along.
        engine.config.configure({"frost_lock_enabled": True})
        self.assertIs(engine.resolve()["afterheat_frost_lock"], True)

    def test_release_needs_thirty_minutes_above_limit_plus_margin(self):
        engine = self.make_engine()
        now = time.time()
        self.readings(engine, outdoor=2.0)
        self.assertTrue(engine.resolve(now)["afterheat_frost_lock"])
        # Above the limit but inside the 2 K margin: still locked, no timer.
        self.readings(engine, outdoor=4.5)
        result = engine.resolve(now + 10)
        self.assertTrue(result["afterheat_frost_lock"])
        self.assertIn("venter på tø", result["afterheat_frost_lock_reason"])
        self.assertIsNone(result["afterheat_frost_lock_release_seconds"])
        self.readings(engine, outdoor=5.5)
        self.assertTrue(engine.resolve(now + 20)["afterheat_frost_lock"])
        # A dip back into the margin restarts the half hour.
        self.readings(engine, outdoor=4.9)
        self.assertTrue(engine.resolve(now + 20 + FROST_LOCK_RELEASE_SECONDS - 60)["afterheat_frost_lock"])
        self.readings(engine, outdoor=5.5)
        start = now + FROST_LOCK_RELEASE_SECONDS
        result = engine.resolve(start)
        self.assertTrue(result["afterheat_frost_lock"])
        self.assertIn("slipper om ca. 30 min", result["afterheat_frost_lock_reason"])
        self.assertTrue(engine.resolve(start + FROST_LOCK_RELEASE_SECONDS - 1)["afterheat_frost_lock"])
        result = engine.resolve(start + FROST_LOCK_RELEASE_SECONDS)
        self.assertFalse(result["afterheat_frost_lock"])
        self.assertIsNone(result["afterheat_frost_lock_reason"])

    def test_every_known_value_must_be_warm_to_release(self):
        engine = self.make_engine()
        now = time.time()
        self.readings(engine, outdoor=1.0)
        engine.resolve(now)
        self.readings(engine, outdoor=12.0, attic=4.0)  # attic inside the margin
        self.assertTrue(engine.resolve(now + 2 * FROST_LOCK_RELEASE_SECONDS)["afterheat_frost_lock"])
        self.assertTrue(engine.resolve(now + 3 * FROST_LOCK_RELEASE_SECONDS)["afterheat_frost_lock"])

    def test_no_known_value_keeps_the_previous_state(self):
        engine = self.make_engine()
        now = time.time()
        self.readings(engine)
        self.assertFalse(engine.resolve(now)["afterheat_frost_lock"])
        self.readings(engine, outdoor=0.0)
        engine.resolve(now)
        self.readings(engine)
        result = engine.resolve(now + 10 * FROST_LOCK_RELEASE_SECONDS)
        self.assertTrue(result["afterheat_frost_lock"])
        self.assertIn("ingen temperaturer", result["afterheat_frost_lock_reason"])

    def test_latch_survives_a_restart(self):
        engine = self.make_engine()
        self.readings(engine, outdoor=-3.0)
        engine.resolve()
        restarted = ControllerEngine(ControllerState(self.state_path), HardwareAdapter())
        self.assertTrue(restarted.resolve()["afterheat_frost_lock"])

    def test_enforces_on_and_floor_over_stored_choices(self):
        writes = []
        engine = self.make_engine(writes=writes)
        engine.config.configure({"afterheat_setpoint": 12})
        engine.config.configure({"afterheat_enabled": False})
        self.readings(engine, outdoor=-2.0)
        result = engine.apply()
        self.assertEqual(writes[-1], 18)
        self.assertEqual(result["afterheat_effective_setpoint"], 18)
        self.assertIn("Frostsikring", result["afterheat_room_reason"])
        # The stored choice is untouched and returns after the thaw.
        self.assertIs(engine.config.data["afterheat_enabled"], False)
        self.assertEqual(engine.config.data["afterheat_setpoint"], 12)
        engine.config.data["frost_lock_latched"] = False
        self.readings(engine, outdoor=12.0)
        engine.apply()
        self.assertIsNone(writes[-1])

    def test_higher_setpoint_and_fractional_floor(self):
        writes = []
        engine = self.make_engine(writes=writes)
        engine.config.configure({"afterheat_setpoint": 22, "frost_lock_min_setpoint_c": 18.5})
        self.readings(engine, outdoor=-2.0)
        engine.apply()
        self.assertEqual(writes[-1], 22)
        with self.assertRaises(ControllerError) as raised:
            engine.config.configure({"afterheat_setpoint": 18})  # 18 is below the 18.5 floor
        self.assertIn("under 18,5 °C", str(raised.exception))
        engine.config.configure({"afterheat_setpoint": 19})
        engine.apply()
        self.assertEqual(writes[-1], 19)

    def test_room_follow_cannot_go_below_floor(self):
        writes = []
        engine = self.make_engine(writes=writes)
        engine.config.configure({"afterheat_room_enabled": True, "afterheat_room_min": 12,
                                 "afterheat_room_target": 20.0, "afterheat_room_step_minutes": 2})
        engine.set_external({"outdoor_temperature": -4.0, "afterheat_room_temperature": 26.0})
        engine.room_cascade.exact = 12.0  # the room loop has walked down to its minimum
        engine.apply()
        self.assertEqual(writes[-1], 18)

    def test_standby_and_bonfire_keep_the_setpoint_while_locked(self):
        for patch in ({"standby_minutes": -1}, {"bonfire_minutes": 60}):
            with self.subTest(patch=patch):
                writes = []
                engine = self.make_engine(writes=writes)
                engine.config.configure(patch)
                self.readings(engine, outdoor=12.0)
                engine.apply()
                self.assertIsNone(writes[-1])
                self.readings(engine, outdoor=-1.0)
                engine.apply()
                self.assertEqual(writes[-1], 20)

    def test_cool_boost_ends_when_the_lock_engages_and_cannot_start(self):
        writes = []
        engine = self.make_engine(writes=writes, set_bypass=lambda _v: None)
        engine.config.configure({"cool_boost_minutes": 60})
        self.readings(engine, outdoor=12.0)
        self.assertTrue(engine.resolve()["cool_boost_active"])
        self.readings(engine, outdoor=2.0)
        result = engine.apply()
        self.assertFalse(result["cool_boost_active"])
        self.assertEqual(result["cool_boost_minutes"], 0)
        self.assertEqual(writes[-1], 20)
        with self.assertRaises(ControllerError) as raised:
            engine.config.configure({"cool_boost_minutes": 30})
        self.assertEqual(str(raised.exception), COOL_ERROR)
        engine.config.configure({"cool_boost_minutes": 0})  # stop is always fine

    def test_patches_that_switch_off_are_refused_with_danish_text(self):
        engine = self.make_engine()
        self.readings(engine, outdoor=-1.0)
        engine.resolve()
        with self.assertRaises(ControllerError) as raised:
            engine.config.configure({"afterheat_enabled": False})
        self.assertEqual(str(raised.exception), OFF_ERROR)
        with self.assertRaises(ControllerError) as raised:
            engine.config.configure({"afterheat_setpoint": 15})
        self.assertEqual(str(raised.exception), FLOOR_ERROR)
        self.assertEqual(engine.config.data["afterheat_setpoint"], 20)
        # Raising, or staying at or above the floor, is fine.
        engine.config.configure({"afterheat_setpoint": 18})
        engine.config.configure({"afterheat_enabled": True, "afterheat_setpoint": 18})

    def test_resent_unchanged_values_are_accepted(self):
        engine = self.make_engine()
        engine.config.configure({"afterheat_setpoint": 14})
        engine.config.configure({"afterheat_enabled": False})
        self.readings(engine, outdoor=-1.0)
        engine.resolve()
        # A form resends every field as it is stored.
        engine.config.configure({"afterheat_enabled": False, "afterheat_setpoint": 14, "cool_boost_minutes": 0})
        engine.config.configure({"afterheat_setpoint": 16})  # raised, still below the floor
        with self.assertRaises(ControllerError):
            engine.config.configure({"afterheat_setpoint": 15})

    def test_admin_may_switch_the_lock_off_in_the_same_patch(self):
        engine = self.make_engine()
        self.readings(engine, outdoor=-1.0)
        engine.resolve()
        engine.config.configure({"frost_lock_enabled": False, "afterheat_enabled": False})
        self.assertFalse(engine.resolve()["afterheat_frost_lock"])

    def test_settings_are_validated_and_admin_only(self):
        engine = self.make_engine()
        for key, bad in (("frost_lock_outdoor_c", 11), ("frost_lock_coil_c", 2.5),
                         ("frost_lock_min_setpoint_c", 26), ("frost_lock_enabled", "ja")):
            with self.subTest(key=key):
                with self.assertRaises(ControllerError):
                    engine.config.configure({key: bad})
        with self.assertRaises(ControllerError):
            engine.config.configure({"frost_lock_latched": False})
        engine.config.configure({"frost_lock_outdoor_c": -2, "frost_lock_coil_c": 5, "frost_lock_min_setpoint_c": 16})
        self.assertEqual(engine.config.data["frost_lock_outdoor_c"], -2.0)
        for key in ("frost_lock_enabled", "frost_lock_outdoor_c", "frost_lock_coil_c", "frost_lock_min_setpoint_c"):
            self.assertEqual(config_allowed("user", {key: 1}), [key])
            self.assertEqual(config_allowed("technician", {key: 1}), [])

    def test_floor_text_uses_the_setting(self):
        engine = self.make_engine()
        engine.config.configure({"frost_lock_min_setpoint_c": 19.5})
        self.readings(engine, outdoor=-1.0)
        engine.resolve()
        with self.assertRaises(ControllerError) as raised:
            engine.config.configure({"afterheat_setpoint": 19})
        self.assertEqual(str(raised.exception),
                         "Frostsikring: eftervarmen kan ikke sættes under 19,5 °C, mens det fryser.")

    def test_afterheat_is_written_first_when_another_write_fails(self):
        writes = []

        def broken(*_args):
            raise RuntimeError("bus busy")

        engine = self.make_engine(writes=writes, set_fireplace=broken)
        self.readings(engine, outdoor=-1.0)
        with self.assertRaisesRegex(RuntimeError, "bus busy"):
            engine.apply()
        self.assertEqual(writes, [20])

    def test_failing_afterheat_write_keeps_retrying_while_locked(self):
        calls = []

        def flaky(value):
            calls.append(value)
            raise RuntimeError("no ack")

        engine = self.make_engine(set_afterheat_setpoint=flaky)
        self.readings(engine, outdoor=-1.0)
        for _ in range(engine.retry_limit + 3):
            with self.assertRaises(RuntimeError):
                engine.apply()
        self.assertEqual(len(calls), engine.retry_limit + 3)


class FrostLockRuntimeTests(unittest.TestCase):
    def make_runtime(self, gateway_state):
        from controller_runtime import ControllerRuntime

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        os.environ["DANTHERM_CHANGE_LOG"] = str(Path(temp.name) / "change-log.jsonl")
        self.addCleanup(os.environ.pop, "DANTHERM_CHANGE_LOG", None)
        runtime = ControllerRuntime(gateway_state=gateway_state, hardware=HardwareAdapter(),
                                    state_path=Path(temp.name) / "controller.json")
        runtime.config.configure({"afterheat_coil": "water"})
        runtime.engine.frost_engage_seconds = 0
        return runtime

    def test_snapshot_keys_enforcement_and_change_log(self):
        state = {"outdoor_temp": 12.0}
        runtime = self.make_runtime(state)
        snapshot = runtime.snapshot()
        self.assertIs(snapshot["afterheat_frost_lock"], False)
        self.assertIs(snapshot["afterheat_frost_lock_enforced"], False)
        state["outdoor_temp"] = -1.6
        snapshot = runtime.snapshot()
        self.assertIs(snapshot["afterheat_frost_lock"], True)
        self.assertEqual(snapshot["afterheat_frost_lock_reason"], "Frost: ude -1,6 °C (grænse 3 °C)")
        self.assertEqual(snapshot["afterheat_frost_lock_floor"], 18.0)
        # HCP4 or no master: the Pi cannot hold the afterheat on.
        self.assertIs(snapshot["afterheat_frost_lock_enforced"], False)
        runtime.master.writes_allowed = lambda: True
        self.assertIs(runtime.snapshot()["afterheat_frost_lock_enforced"], True)
        event = runtime.change_log[-1]
        self.assertEqual(event["source"], "Frostsikring")
        self.assertEqual(event["changes"], {"afterheat_frost_lock": [False, True]})
        self.assertIn("ude -1,6", event["reason"])

    def test_attic_role_and_frost_sensor_reach_the_engine(self):
        state = {"outdoor_temp": 12.0, "heating_coil_frost_temperature": 6.0,
                 "heating_coil_frost_temperature_sample_monotonic": time.monotonic()}
        runtime = self.make_runtime(state)
        runtime.onewire.by_role = lambda role, now=None: 1.0 if role == "attic" else None
        snapshot = runtime.snapshot()
        self.assertTrue(snapshot["afterheat_frost_lock"])
        self.assertIn("loft 1,0 °C", snapshot["afterheat_frost_lock_reason"])
        self.assertIn("frostføler 6,0 °C", snapshot["afterheat_frost_lock_reason"])

    def test_capped_outdoor_word_means_no_hac1_lockout(self):
        runtime = self.make_runtime({"outdoor_temp": 16.0})
        self.assertIs(runtime.snapshot()["actual_afterheat_outdoor_lockout"], True)
        runtime.onewire.by_role = lambda role, now=None: 0.5 if role == "attic" else None
        # Under HCP4 the Pi does not write register 180, so HAC1 still sees 16 C.
        self.assertIs(runtime.snapshot()["actual_afterheat_outdoor_lockout"], True)
        runtime.master.writes_allowed = lambda: True
        self.assertIs(runtime.snapshot()["actual_afterheat_outdoor_lockout"], False)

    def test_pi_rejection_is_a_controller_error(self):
        runtime = self.make_runtime({"outdoor_temp": -1.0})
        runtime.snapshot()
        with self.assertRaises(ControllerError) as raised:
            runtime.configure({"afterheat_enabled": False}, apply=False)
        self.assertEqual(str(raised.exception), OFF_ERROR)


class FrostLockGatewayTests(unittest.TestCase):
    def test_register_180_outdoor_is_capped_while_locked(self):
        from types import SimpleNamespace

        sys.path.insert(0, str(Path(__file__).parent))
        from test_bypass_and_discovery import make_gateway

        gateway = make_gateway()
        gateway.state.update({"outdoor_temp": 16.5, "supply_temp": 20.0, "extract_temp": 21.0,
                              "exhaust_temp": 10.0})
        gateway.read_register_block = lambda _ser, slave, start, count: [0, 0, 0, 0, 2200]
        gateway.controller = SimpleNamespace(engine=SimpleNamespace(frost_lock_active=False))
        self.assertEqual(gateway._afterheat_temperature_words(None)[0], 1650)
        gateway.controller.engine.frost_lock_active = True
        self.assertEqual(gateway._afterheat_temperature_words(None)[0], 1000)
        gateway.state["outdoor_temp"] = -2.5
        self.assertEqual(gateway._afterheat_temperature_words(None)[0], round(-250) & 0xFFFF)


if __name__ == "__main__":
    unittest.main()
