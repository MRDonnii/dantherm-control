"""A room's own CO2 limit (1.13.0-beta.17): Smart Auto votes, dry air, night and winter."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "gateway"))
from controller_core import ControllerEngine, ControllerError, ControllerState, HardwareAdapter  # noqa: E402
from controller_runtime import ControllerRuntime  # noqa: E402
import room_setup  # noqa: E402

T0 = 1_000_000.0


def room(name, room_type="normal", **extra):
    return {"name": name, "type": room_type, **extra}


class ValidationTests(unittest.TestCase):
    def test_limit_is_optional_and_checked(self):
        self.assertEqual(room_setup.clean_room(room("Køkken", "kitchen", co2_limit=1200))["co2_limit"], 1200)
        self.assertNotIn("co2_limit", room_setup.clean_room(room("Køkken", "kitchen", co2_limit="")))
        self.assertNotIn("co2_limit", room_setup.clean_room(room("Køkken", "kitchen")))
        for bad in (599, 2001, "mange"):
            with self.assertRaises(room_setup.RoomSetupError):
                room_setup.clean_room(room("Køkken", "kitchen", co2_limit=bad))
        # Only rooms that control have a CO2 limit.
        self.assertNotIn("co2_limit", room_setup.clean_room(room("Ude", "outdoor", co2_limit=1200)))

    def test_config_refuses_a_bad_limit(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        state = ControllerState(Path(temp.name) / "controller.json")
        with self.assertRaises(ControllerError):
            state.configure({"room_setup": [room("Køkken", "kitchen", co2_limit=3000)]})
        state.configure({"room_setup": [room("Køkken", "kitchen", co2_limit=1200), room("Stue")]})
        self.assertEqual([r.get("co2_limit") for r in state.data["room_setup"]], [1200, None])


class SmartAutoTests(unittest.TestCase):
    def make_runtime(self, setup):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        runtime = ControllerRuntime(gateway_state={}, hardware=HardwareAdapter(),
                                    state_path=Path(temp.name) / "controller.json")
        # Base level 2 on the four-step unit, so +1 and +2 both fit under the top.
        runtime.config.configure({"room_setup": setup, "local_normal_level": 2, "local_min_level": 1})
        return runtime

    def decide(self, runtime, rooms):
        runtime.room_inputs({"source": "home_assistant", "valid_for_s": 180, "rooms": rooms})
        return runtime._derive_smart_decision()

    def test_kitchen_votes_from_its_own_limit_and_other_rooms_are_unchanged(self):
        runtime = self.make_runtime([room("Køkken", "kitchen", co2_limit=1200), room("Stue")])
        normal = runtime.config.normal_level()
        # 1000 is like 600 for the house (under limit - hysteresis): no upshift, the low band as usual.
        self.assertEqual(self.decide(runtime, {"Køkken": {"co2": 1000}})[0], normal - 1)
        self.assertEqual(self.decide(runtime, {"Køkken": {"co2": 1150}})[0], normal)
        self.assertEqual(self.decide(runtime, {"Køkken": {"co2": 1200}})[0], normal)  # over the limit, as the house setpoint
        level, _demand, reason, room_name, _metric = self.decide(runtime, {"Køkken": {"co2": 1250}})
        self.assertEqual((level, room_name), (normal + 1, "Køkken"))
        self.assertEqual(reason, "CO2 Køkken 1250 (grænse 1200) (auto)")
        self.assertEqual(self.decide(runtime, {"Køkken": {"co2": 1450}})[0], normal + 2)
        # Stue has no limit: the house's 800 ppm as before, and the old reason text.
        level, _demand, reason, room_name, _metric = self.decide(runtime, {"Køkken": {"co2": 1000}, "Stue": {"co2": 850}})
        self.assertEqual((level, room_name, reason), (normal + 1, "Stue", "CO2 Stue 850 (auto)"))

    def test_without_a_limit_nothing_changes(self):
        runtime = self.make_runtime([room("Køkken", "kitchen")])
        normal = runtime.config.normal_level()
        self.assertEqual(self.decide(runtime, {"Køkken": {"co2": 850}})[:3:2], (normal + 1, "CO2 Køkken 850 (auto)"))
        signals = runtime._room_co2_signals()
        self.assertEqual(signals["max_room_co2_effective"], 850)
        self.assertIsNone(signals["max_room_co2_room"])
        self.assertIsNone(signals["unit_co2_room_limit"])

    def test_signals_shift_a_room_with_a_limit(self):
        runtime = self.make_runtime([room("Køkken", "kitchen", co2_limit=1200), room("Stue")])
        self.decide(runtime, {"Køkken": {"co2": 1250}, "Stue": {"co2": 820}})
        signals = runtime._room_co2_signals()
        self.assertEqual((signals["max_room_co2_effective"], signals["max_room_co2_room"],
                          signals["max_room_co2_room_value"], signals["max_room_co2_room_limit"]),
                         (850, "Køkken", 1250, 1200))
        self.decide(runtime, {"Køkken": {"co2": 1000}, "Stue": {"co2": 820}})
        signals = runtime._room_co2_signals()
        self.assertEqual((signals["max_room_co2_effective"], signals["max_room_co2_room"]), (820, None))
        self.assertEqual(runtime._max_room_co2(), 1000)  # the raw maximum is still shown and logged

    def test_unit_sensor_room_uses_its_limit(self):
        # Spisestue has no HA CO2 sensor: its CO2 comes from the unit's own sensor.
        runtime = self.make_runtime([room("Spisestue", unit_sensor=True, co2_limit=1200), room("Stue")])
        runtime.gateway_state.update({"co2": 1000})
        normal = runtime.config.normal_level()
        level, _demand, _reason, _room, _metric = self.decide(runtime, {"Spisestue": {"temperature": 21.0}})
        self.assertEqual(runtime._rooms_with_unit_sensors()["Spisestue"]["co2"], 1000)
        self.assertEqual(level, normal - 1)  # 800 ppm would have given normal + 1
        runtime.gateway_state.update({"co2": 1250})
        level, _demand, reason, room_name, _metric = self.decide(runtime, {"Spisestue": {"temperature": 21.0}})
        self.assertEqual((level, room_name, reason), (normal + 1, "Spisestue", "CO2 Spisestue 1250 (grænse 1200) (auto)"))
        signals = runtime._room_co2_signals()
        self.assertEqual((signals["unit_co2_room"], signals["unit_co2_room_limit"]), ("Spisestue", 1200))


class EngineTests(unittest.TestCase):
    """The engine side: dry-air release/restart, night and winter with the shifted CO2."""

    def make_engine(self, **config):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        state = ControllerState(Path(temp.name) / "controller.json")
        state.configure({"dry_protection_enabled": True, "dry_dynamic_enabled": True, "dry_hold_minutes": 60,
                         "local_min_level": 1, "dry_min_level": 1, "local_normal_level": 4, **config})
        return ControllerEngine(state, HardwareAdapter())

    def step(self, engine, now, *, kitchen=None, unit_co2=500, unit_limit=None, rh=30.0):
        """Køkken has limit 1200 (house 800), so it arrives as CO2 - 400, as the runtime sends it."""
        engine.update_measurements(rh=None, co2=unit_co2, outdoor=2.0)
        external = {"indoor_rh_average": rh, "indoor_rh_lowest": rh, "indoor_temperature_average": 22.0,
                    "dry_outdoor_temperature": 2.0, "dry_outdoor_rh": 85.0, "dry_outdoor_source": "udeluft",
                    "max_room_co2": kitchen}
        if kitchen is not None:
            external.update({"max_room_co2_effective": kitchen - 400, "max_room_co2_room": "Køkken",
                             "max_room_co2_room_value": kitchen, "max_room_co2_room_limit": 1200})
        if unit_limit is not None:
            external.update({"unit_co2_room": "Spisestue", "unit_co2_room_limit": unit_limit})
        engine.set_external(external)
        return engine.resolve(now)

    def test_dry_air_release_and_restart_follow_the_room_limit(self):
        engine = self.make_engine()
        self.assertEqual(self.step(engine, T0)["dry_tier"], 1)
        # 1150 in the kitchen is under its 1200: the protection stays.
        self.assertEqual(self.step(engine, T0 + 5, kitchen=1150)["dry_tier"], 1)
        result = self.step(engine, T0 + 10, kitchen=1250)
        self.assertEqual((result["dry_tier"], result["dry_protection_state"]), (0, "co2_high"))
        self.assertEqual(result["dry_reason"], "Slået fra: CO₂ Køkken 1250 ppm er over 1200 ppm (grænse 1200)")
        # Restart only at the room's limit - hysteresis = 1100.
        later = T0 + 10 + 3600
        result = self.step(engine, later, kitchen=1150)
        self.assertEqual(result["dry_protection_state"], "co2_wait")
        self.assertEqual(result["dry_reason"], "Venter: CO₂ Køkken 1150 ppm skal ned på 1100 ppm (grænse 1200), før den starter igen")
        self.assertEqual(self.step(engine, later + 5, kitchen=1090)["dry_tier"], 1)

    def test_soft_co2_release_moves_with_the_room_limit(self):
        # Soft CO2: the house releases at 1200 / restarts at 1100; Køkken at 1600 / 1500.
        engine = self.make_engine(dry_co2_soft_enabled=True)
        self.assertEqual(self.step(engine, T0)["dry_tier"], 1)
        result = self.step(engine, T0 + 5, kitchen=1250)
        self.assertEqual((result["dry_tier"], result["dry_cap_level"]), (1, 3))
        self.assertIn("CO₂ Køkken 1250 ppm løfter ét trin", result["dry_reason"])
        self.assertEqual(self.step(engine, T0 + 10, kitchen=1550)["dry_tier"], 1)
        result = self.step(engine, T0 + 15, kitchen=1650)
        self.assertEqual(result["dry_tier"], 0)
        self.assertIn("over 1600 ppm (grænse 1200)", result["dry_reason"])
        later = T0 + 15 + 3600
        self.assertEqual(self.step(engine, later, kitchen=1550)["dry_tier"], 0)
        self.assertEqual(self.step(engine, later + 5, kitchen=1480)["dry_tier"], 1)

    def test_unit_sensor_room_limit_in_dry_air(self):
        engine = self.make_engine()
        self.assertEqual(self.step(engine, T0, unit_co2=1000, unit_limit=1200)["dry_tier"], 1)
        result = self.step(engine, T0 + 5, unit_co2=1000)  # no room limit: 1000 > 800 releases as before
        self.assertEqual((result["dry_tier"], result["dry_protection_state"]), (0, "co2_high"))
        self.assertEqual(result["dry_reason"], "Slået fra: CO₂ 1000 ppm er over 800 ppm")

    def test_night_urgency_uses_the_unit_sensor_room_limit(self):
        engine = self.make_engine(dry_protection_enabled=False, night_enabled=True, night_start="00:00",
                                  night_end="00:00", night_level=2)
        result = self.step(engine, T0, unit_co2=1000, unit_limit=1200)
        self.assertNotEqual(result["effective_source"], "night_air_quality")
        result = self.step(engine, T0 + 5, unit_co2=1000)
        self.assertEqual(result["effective_source"], "night_air_quality")

    def test_winter_pause_names_the_room(self):
        engine = self.make_engine()
        engine.update_measurements(rh=None, co2=500, outdoor=2.0)
        engine.set_external({"max_room_co2": 1250, "max_room_co2_effective": 850, "max_room_co2_room": "Køkken",
                             "max_room_co2_room_value": 1250, "max_room_co2_room_limit": 1200})
        self.assertIsNone(engine._winter_pause_trigger(engine.config.data, T0))
        engine.set_external({"max_room_co2": 1350, "max_room_co2_effective": 950, "max_room_co2_room": "Køkken",
                             "max_room_co2_room_value": 1350, "max_room_co2_room_limit": 1200})
        self.assertEqual(engine._winter_pause_trigger(engine.config.data, T0),
                         "CO2 Køkken 1350 ppm over 1300 ppm (grænse 1200)")


if __name__ == "__main__":
    unittest.main()
