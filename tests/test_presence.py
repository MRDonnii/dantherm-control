"""Ventilation after occupancy: presence per room, home/away from HA."""
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "gateway"))

import room_setup
from controller_core import ControllerEngine, ControllerState, HardwareAdapter
from controller_runtime import ControllerRuntime
from occupancy import OccupancyTracker


def at(hour, minute=0):
    return datetime(2027, 3, 10, hour, minute).astimezone().timestamp()


class OccupancyTrackerTests(unittest.TestCase):
    def test_on_and_off_hold_times(self):
        tracker = OccupancyTracker()
        tracker.observe("Stue", True, 0)
        self.assertFalse(tracker.update(4 * 60, 5, 15)["Stue"]["occupied"])
        tracker.observe("Stue", True, 4 * 60)
        self.assertTrue(tracker.update(5 * 60, 5, 15)["Stue"]["occupied"])
        for minute in range(6, 22):  # HA reports every minute
            tracker.observe("Stue", False, minute * 60)
        self.assertTrue(tracker.update(20 * 60, 5, 15)["Stue"]["occupied"])
        self.assertFalse(tracker.update(21 * 60, 5, 15)["Stue"]["occupied"])

    def test_short_radar_dropouts_do_not_break_a_streak(self):
        tracker = OccupancyTracker()
        tracker.observe("Stue", True, 0)
        tracker.observe("Stue", False, 120)
        tracker.observe("Stue", True, 180)  # 60 s gap
        self.assertTrue(tracker.update(300, 5, 15)["Stue"]["occupied"])
        tracker = OccupancyTracker()
        tracker.observe("Stue", True, 0)
        tracker.observe("Stue", False, 60)
        tracker.observe("Stue", True, 240)  # 3 min gap starts over
        self.assertFalse(tracker.update(300, 5, 15)["Stue"]["occupied"])

    def test_unknown_and_stale_never_count(self):
        tracker = OccupancyTracker()
        tracker.observe("Stue", True, 0)
        self.assertTrue(tracker.update(400, 5, 15)["Stue"]["occupied"])
        tracker.observe("Stue", None, 500)
        self.assertFalse(tracker.update(510, 5, 15)["Stue"]["occupied"])
        tracker.observe("Kontor", True, 0)
        self.assertFalse(tracker.update(2000, 5, 15)["Kontor"]["occupied"])


class PresenceEngineTests(unittest.TestCase):
    def make_engine(self, **settings):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        state = ControllerState(Path(temp.name) / "controller.json")
        state.configure(settings)
        return ControllerEngine(state, HardwareAdapter())

    def signals(self, engine, home=None, occupied=()):
        engine.set_external({"home_state": home, "occupied_rooms": list(occupied), "presence_rooms": 6})

    def test_off_by_default(self):
        engine = self.make_engine()
        self.signals(engine, home="away")
        engine.update_measurements(rh=45, co2=600)
        result = engine.resolve(at(12))
        self.assertEqual(result["presence_state"], "disabled")
        self.assertEqual(result["effective_level"], 3)
        self.assertIs(result["presence_away_enabled"], False)
        self.assertIs(result["presence_busy_enabled"], False)

    def test_away_limits_the_level(self):
        engine = self.make_engine(presence_away_enabled=True)
        self.signals(engine, home="away")
        engine.update_measurements(rh=45, co2=600)
        result = engine.resolve(at(12))
        self.assertEqual(result["effective_level"], 2)
        self.assertEqual(result["effective_source"], "presence_away")
        self.assertEqual(result["presence_state"], "away")
        self.assertTrue(result["presence_away_active"])

    def test_away_keeps_air_quality_demand(self):
        engine = self.make_engine(presence_away_enabled=True)
        self.signals(engine, home="away")
        engine.update_measurements(rh=45, co2=1400)
        result = engine.resolve(at(12))
        self.assertGreater(result["effective_level"], 3)
        self.assertEqual(result["presence_state"], "away_demand")

    def test_many_occupied_rooms_raise_the_level_by_one(self):
        engine = self.make_engine(presence_busy_enabled=True)
        self.signals(engine, home="home", occupied=["Stue", "Køkken", "Kontor"])
        engine.update_measurements(rh=45, co2=600)
        self.assertEqual(engine.resolve(at(12))["effective_level"], 3)
        self.signals(engine, home="home", occupied=["Stue", "Køkken", "Kontor", "Spisestue"])
        result = engine.resolve(at(12, 1))
        self.assertEqual(result["effective_level"], 4)
        self.assertEqual(result["effective_source"], "presence_busy")
        self.assertEqual(result["occupied_count"], 4)
        self.assertIn("4 rum i brug", result["effective_reason"])

    def test_toggles_are_separate(self):
        engine = self.make_engine(presence_busy_enabled=True)
        self.signals(engine, home="away")
        engine.update_measurements(rh=45, co2=600)
        self.assertEqual(engine.resolve(at(12))["effective_level"], 3)  # away limit is off
        engine = self.make_engine(presence_away_enabled=True)
        self.signals(engine, home="home", occupied=["A", "B", "C", "D", "E"])
        engine.update_measurements(rh=45, co2=600)
        self.assertEqual(engine.resolve(at(12))["effective_level"], 3)  # busy raise is off

    def test_night_keeps_its_limits(self):
        engine = self.make_engine(presence_busy_enabled=True, night_enabled=True, night_start="22:00",
                                  night_end="06:00", night_level=2)
        self.signals(engine, home="home", occupied=["Soveværelse", "Mads", "Viggo", "Stue"])
        engine.update_measurements(rh=45, co2=600)
        result = engine.resolve(at(2))
        self.assertEqual(result["effective_level"], 2)
        self.assertEqual(result["presence_state"], "busy_night")


class PresenceRuntimeTests(unittest.TestCase):
    def make_runtime(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        runtime = ControllerRuntime(gateway_state={"extract_temp": 21.0}, hardware=HardwareAdapter(),
                                    state_path=Path(temp.name) / "controller.json")
        runtime.hardware_writes_allowed = lambda: False
        runtime.configure({"room_setup": [
            {"name": "Stue", "type": "normal", "area_id": "stue",
             "sensors": {"temperature": "sensor.stue_t", "presence": "binary_sensor.hps_stuen_presence"}},
            {"name": "Bad", "type": "bathroom", "sensors": {"humidity": "sensor.bad_rh", "presence": "binary_sensor.bad_p"}},
            {"name": "Kontor", "type": "normal", "area_id": "kontor"},
        ]})
        return runtime

    def test_presence_only_room_is_accepted_and_counted_after_the_hold(self):
        runtime = self.make_runtime()
        runtime.room_inputs({"valid_for_s": 180, "rooms": {
            "Stue": {"temperature": 21.0, "presence": True}, "Bad": {"humidity": 50, "presence": True},
            "Kontor": {"presence": True}}})
        self.assertIn("Kontor", runtime.smart_rooms)
        now = time.time()
        self.assertEqual(runtime._occupied_rooms(now), ([], 3))
        # Bathrooms never count as rooms in use.
        for room in runtime.occupancy.rooms.values():
            room.on_since -= 301  # five minutes of presence
        self.assertEqual(runtime._occupied_rooms(now), (["Kontor", "Stue"], 3))

    def test_presence_must_be_boolean(self):
        runtime = self.make_runtime()
        with self.assertRaises(Exception):
            runtime.room_inputs({"valid_for_s": 180, "rooms": {"Stue": {"temperature": 21.0, "presence": "on"}}})

    def test_catalog_picks_a_presence_sensor_per_room(self):
        runtime = self.make_runtime()
        runtime.ha_catalog_input({"areas": [
            {"area_id": "kontor", "area_name": "Kontor", "sensors": [
                {"entity_id": "sensor.kontor_t", "kind": "temperature"},
                {"entity_id": "binary_sensor.hps_kontor_presence", "kind": "presence"},
                {"entity_id": "binary_sensor.hps_kontor_radar_moving_target", "kind": "presence"}]},
            {"area_id": "stue", "area_name": "Stue", "sensors": [
                {"entity_id": "binary_sensor.other", "kind": "presence"}]},
        ]})
        rooms = {room["name"]: room for room in runtime.config.data["room_setup"]}
        self.assertEqual(rooms["Kontor"]["sensors"]["presence"], "binary_sensor.hps_kontor_presence")
        self.assertEqual(rooms["Stue"]["sensors"]["presence"], "binary_sensor.hps_stuen_presence")  # kept

    def test_none_chosen_is_respected(self):
        rooms = [room_setup.clean_room({"name": "Kontor", "area_id": "kontor", "presence_auto": False})]
        catalog = [{"area_id": "kontor", "sensors": [{"entity_id": "binary_sensor.p", "kind": "presence"}]}]
        self.assertEqual(room_setup.fill_presence(rooms, catalog)[1], [])

    def test_presence_must_be_a_binary_sensor(self):
        with self.assertRaises(room_setup.RoomSetupError):
            room_setup.clean_room({"name": "Stue", "sensors": {"presence": "sensor.stue"}})
        with self.assertRaises(room_setup.RoomSetupError):
            room_setup.clean_room({"name": "Stue", "sensors": {"temperature": "binary_sensor.stue"}})
        cleaned = room_setup.clean_catalog([{"area_id": "a", "sensors": [
            {"entity_id": "binary_sensor.p", "kind": "presence"}, {"entity_id": "binary_sensor.t", "kind": "temperature"}]}])
        self.assertEqual([s["entity_id"] for s in cleaned[0]["sensors"]], ["binary_sensor.p"])

    def test_extract_label(self):
        self.assertEqual(room_setup.ROOM_TYPE_LABELS["extract"], "Kun kanalmåling (udsugning)")


if __name__ == "__main__":
    unittest.main()
