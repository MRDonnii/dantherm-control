"""Afterheat supply target from the rooms (room_cascade and its use in the controller)."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "gateway"))

from controller_core import ControllerEngine, ControllerError, ControllerState, HardwareAdapter
from controller_runtime import ControllerRuntime
from room_cascade import RoomCascade

SETTINGS = {
    "afterheat_room_target": 22.0, "afterheat_room_min": 17, "afterheat_room_max": 24,
    "afterheat_room_step_minutes": 10, "afterheat_room_gain": 1.5, "afterheat_room_integral_minutes": 180.0,
    "afterheat_room_max_step": 0.2, "afterheat_room_band_k": 0.3,
}


def run(cascade, room, steps, start=0.0, **kwargs):
    value = None
    for index in range(steps + 1):
        value = cascade.update(now=start + index * 600, room=room, settings=SETTINGS, base=21.0, **kwargs)
    return value


class RoomCascadeTests(unittest.TestCase):
    def test_starts_at_the_base_and_steps_at_most_the_max_step(self):
        cascade = RoomCascade()
        self.assertEqual(cascade.update(now=0, room=20.0, settings=SETTINGS, base=21.0), 21.0)
        self.assertEqual(cascade.update(now=300, room=20.0, settings=SETTINGS, base=21.0), 21.0)  # waits
        # 2 K too cold: 1.5 * 10/180 * 2 = 0.17 K per step.
        self.assertEqual(cascade.update(now=600, room=20.0, settings=SETTINGS, base=21.0), 21.2)
        self.assertEqual(cascade.state, "rising")
        self.assertEqual(cascade.update(now=1200, room=20.0, settings=SETTINGS, base=21.0), 21.3)
        # A sudden 1 K drop adds the proportional part, but never more than 0.2 K per step.
        cascade.update(now=1800, room=19.0, settings=SETTINGS, base=21.0)
        self.assertAlmostEqual(cascade.exact, 21.533, places=2)

    def test_small_errors_add_up_instead_of_rounding_away(self):
        cascade = RoomCascade()
        # 0.5 K too cold: 1.5 * 10/180 * 0.5 = 0.042 K per step.
        value = run(cascade, 21.5, 6)
        self.assertAlmostEqual(cascade.exact, 21.25, places=2)
        self.assertEqual(value, 21.3)

    def test_inside_the_dead_band_nothing_moves(self):
        cascade = RoomCascade()
        self.assertEqual(run(cascade, 21.8, 12), 21.0)
        self.assertEqual(cascade.state, "in_band")
        cascade = RoomCascade()
        self.assertEqual(run(cascade, 22.25, 12), 21.0)

    def test_warm_rooms_lower_the_target_down_to_the_minimum(self):
        cascade = RoomCascade()
        self.assertEqual(run(cascade, 25.0, 60), 17.0)
        self.assertEqual(cascade.state, "at_limit")
        # Anti-windup: the moment the rooms are a little cold it starts rising again.
        self.assertEqual(cascade.update(now=61 * 600, room=21.0, settings=SETTINGS, base=21.0), 17.2)

    def test_saturated_afterheat_does_not_push_further(self):
        cascade = RoomCascade()
        run(cascade, 20.0, 2)
        before = cascade.exact
        run(cascade, 20.0, 5, start=3 * 600, saturated="high")
        self.assertEqual(cascade.exact, before)
        self.assertEqual(cascade.state, "saturated")
        # Saturated high but the rooms too warm: lowering is still allowed.
        run(cascade, 24.0, 1, start=10 * 600, saturated="high")
        self.assertLess(cascade.exact, before)

    def test_locked_freezes_the_target(self):
        cascade = RoomCascade()
        run(cascade, 20.0, 2)
        value = cascade.value
        self.assertEqual(run(cascade, 18.0, 6, start=3 * 600, locked="frostsikring"), value)
        self.assertEqual(cascade.state, "locked")

    def test_no_room_temperature_gives_none(self):
        self.assertIsNone(RoomCascade().update(now=0, room=None, settings=SETTINGS, base=21.0))


class RoomCascadeEngineTests(unittest.TestCase):
    def make_engine(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        state = ControllerState(Path(temp.name) / "controller.json")
        return ControllerEngine(state, HardwareAdapter())

    def test_off_by_default_with_the_new_defaults(self):
        engine = self.make_engine()
        result = engine.resolve(now=1000.0)
        self.assertIs(result["afterheat_room_enabled"], False)
        self.assertEqual(result["afterheat_room_target"], 22.0)
        self.assertEqual((result["afterheat_room_min"], result["afterheat_room_max"]), (17, 24))
        self.assertEqual(result["afterheat_room_band_k"], 0.3)
        self.assertEqual(result["afterheat_room_offset_k"], 0.0)
        self.assertIsNone(result["afterheat_room_supply_target"])

    def test_duct_offset_is_added_and_shown(self):
        engine = self.make_engine()
        engine.config.configure({"afterheat_setpoint": 22, "afterheat_room_enabled": True,
                                 "afterheat_room_offset_k": 0.5})
        engine.set_external({"afterheat_room_temperature": 22.2})
        result = engine.resolve(now=1000.0)
        # Starts from the fixed setpoint at the unit: 21.5 at the rooms + 0.5 duct loss.
        self.assertEqual(result["afterheat_room_rooms_target"], 21.5)
        self.assertEqual(result["afterheat_room_supply_target"], 22.0)
        self.assertEqual(result["afterheat_target"], 22.0)
        self.assertIn("22,2 °C i rummene → indblæsning 21,5 °C (+0,5 K kanaltab = 22,0 °C ved anlægget)",
                      result["afterheat_room_reason"])
        self.assertIn("inden for ±0,3 K af 22,0 °C", engine.resolve(now=1600.0)["afterheat_room_reason"])

    def test_cool_boost_freezes_the_room_loop(self):
        engine = self.make_engine()
        engine.config.configure({"afterheat_room_enabled": True, "cool_boost_minutes": 30})
        engine.set_external({"afterheat_room_temperature": 19.0, "outdoor_temperature": 15.0})
        first = engine.resolve(now=1000.0)["afterheat_room_supply_target"]
        result = engine.resolve(now=1000.0 + 3600)
        self.assertEqual(result["afterheat_room_supply_target"], first)
        self.assertEqual(result["afterheat_room_state"], "locked")
        self.assertEqual(result["afterheat_effective_setpoint"], 10)

    def test_saturation_comes_from_the_afterheat_control(self):
        engine = self.make_engine()
        engine.last_pilot = {"afterheat_level": 10, "afterheat_control_error": -1.2}
        self.assertEqual(engine._afterheat_saturation(), "high")
        engine.last_pilot = {"afterheat_level": 0, "afterheat_control_error": 0.8}
        self.assertEqual(engine._afterheat_saturation(), "low")
        engine.last_pilot = {"afterheat_level": 5, "afterheat_control_error": -1.2}
        self.assertIsNone(engine._afterheat_saturation())

    def test_settings_are_validated(self):
        engine = self.make_engine()
        for patch in ({"afterheat_room_band_k": 2.0}, {"afterheat_room_offset_k": 4.0},
                      {"afterheat_room_max_step": 0.05}, {"afterheat_room_integral_minutes": 10}):
            with self.subTest(patch=patch), self.assertRaises(ControllerError):
                engine.config.configure(patch)

    def test_old_unused_room_target_gets_the_new_default(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "controller.json"
        path.write_text('{"afterheat_room_enabled": false, "afterheat_room_target": 21.0}', encoding="utf-8")
        self.assertEqual(ControllerState(path).data["afterheat_room_target"], 22.0)
        path.write_text('{"afterheat_room_enabled": true, "afterheat_room_target": 21.0}', encoding="utf-8")
        self.assertEqual(ControllerState(path).data["afterheat_room_target"], 21.0)
        path.write_text('{"afterheat_room_v2": true, "afterheat_room_target": 21.5}', encoding="utf-8")
        self.assertEqual(ControllerState(path).data["afterheat_room_target"], 21.5)


class RoomAverageTests(unittest.TestCase):
    def make_runtime(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        runtime = ControllerRuntime(gateway_state={"extract_temp": 21.0}, hardware=HardwareAdapter(),
                                    state_path=Path(temp.name) / "controller.json")
        runtime.hardware_writes_allowed = lambda: False
        rooms = [
            {"name": "Stue", "type": "normal", "priority": "high", "sensors": {"temperature": "sensor.stue_t"}},
            {"name": "Soveværelse", "type": "bedroom", "sensors": {"temperature": "sensor.sove_t"}},
            {"name": "Køkken", "type": "kitchen", "priority": "low", "sensors": {"temperature": "sensor.k_t"}},
            {"name": "Bad", "type": "bathroom", "sensors": {"temperature": "sensor.bad_t"}},
            {"name": "Bryggers", "type": "extract", "sensors": {"temperature": "sensor.b_t"}},
            {"name": "Kontor", "type": "normal", "control": False, "sensors": {"temperature": "sensor.ko_t"}},
            {"name": "Udeluft", "type": "outdoor", "sensors": {"temperature": "sensor.ude_t"}},
        ]
        runtime.configure({"room_setup": rooms})
        return runtime

    def send(self, runtime, **extra):
        rooms = {"Stue": {"temperature": 22.0}, "Soveværelse": {"temperature": 20.0},
                 "Køkken": {"temperature": 24.0}, "Bad": {"temperature": 27.0},
                 "Bryggers": {"temperature": 15.0}, "Kontor": {"temperature": 30.0}, "Udeluft": {"temperature": 9.0}}
        for name, values in extra.items():
            rooms[name].update(values)
        runtime.room_inputs({"source": "home_assistant", "valid_for_s": 180, "rooms": rooms})
        runtime.refresh_measurements()

    def test_average_uses_living_rooms_with_control_on(self):
        runtime = self.make_runtime()
        self.send(runtime)
        self.assertEqual(runtime._comfort_rooms_temperature(), (22.0, 3))
        self.assertEqual(runtime.engine.external["afterheat_room_temperature"], 22.0)
        self.assertEqual(runtime.engine.external["outdoor_air_temperature"], 9.0)
        self.assertEqual(runtime.snapshot()["afterheat_room_source_used"], "rooms")

    def test_weighted_by_priority(self):
        runtime = self.make_runtime()
        runtime.configure({"afterheat_room_weighted": True})
        self.send(runtime)
        # (22*1.5 + 20*1 + 24*0.5) / 3 = 21.67
        self.assertEqual(runtime._comfort_rooms_temperature()[0], 21.67)

    def test_stale_room_temperature_is_skipped(self):
        runtime = self.make_runtime()
        self.send(runtime, Køkken={"temperature_age_s": 4 * 3600})
        self.assertEqual(runtime._comfort_rooms_temperature(), (21.0, 2))


if __name__ == "__main__":
    unittest.main()
