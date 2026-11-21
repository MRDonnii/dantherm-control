import sys
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "gateway"))

from api_keys import ENDPOINT_SCOPES  # noqa: E402
from controller_core import ControllerError, ControllerState, HardwareAdapter  # noqa: E402
from controller_runtime import ControllerRuntime  # noqa: E402
import room_setup  # noqa: E402

CATALOG = [
    {"area_id": "stue", "area_name": "Stue", "sensors": [
        {"entity_id": "sensor.temp_fugtighed_stue_temperature", "name": "Stue temperatur", "kind": "temperature"},
        {"entity_id": "sensor.temp_fugtighed_stue_humidity", "name": "Stue fugt", "kind": "humidity"},
        {"entity_id": "sensor.braendeovn_temp_fugtighed_temperature", "name": "Brændeovn", "kind": "temperature"},
    ]},
    {"area_id": "sovevaerelse", "area_name": "Soveværelse", "sensors": [
        {"entity_id": "sensor.luftkvalitet_sovevaerelse_carbon_dioxide", "name": "CO2", "kind": "co2"},
    ]},
    {"area_id": "garage", "area_name": "Garage", "sensors": [
        {"entity_id": "sensor.garage_temperature", "name": "Garage", "kind": "temperature"},
    ]},
]


def room(name, room_type="normal", **extra):
    return {"name": name, "type": room_type, **extra}


class RoomSetupValidationTests(unittest.TestCase):
    def test_clean_room_defaults_and_id(self):
        cleaned = room_setup.clean_room({"name": "Soveværelse", "sensors": {"co2": "sensor.co2"}})
        self.assertEqual(cleaned["id"], "sovevaerelse")
        self.assertEqual(cleaned["type"], "normal")
        self.assertEqual(cleaned["priority"], "auto")
        self.assertTrue(cleaned["control"])
        self.assertFalse(cleaned["unit_sensor"])
        self.assertEqual(cleaned["sensors"], {"temperature": None, "humidity": None, "co2": "sensor.co2", "pm25": None})

    def test_presence_from_1_12_0_is_dropped_silently(self):
        cleaned = room_setup.clean_room({"name": "Stue", "presence_auto": False,
                                         "sensors": {"temperature": "sensor.t", "presence": "binary_sensor.radar"}})
        self.assertNotIn("presence", cleaned["sensors"])
        self.assertNotIn("presence_auto", cleaned)
        catalog = room_setup.clean_catalog([{"area_id": "stue", "sensors": [
            *({"entity_id": f"binary_sensor.radar_{i}", "kind": "presence"} for i in range(8)),
            *({"entity_id": f"sensor.t_{i}", "kind": "temperature"} for i in range(40)),
        ]}])
        self.assertEqual(len(catalog[0]["sensors"]), 40)
        self.assertTrue(all(sensor["kind"] == "temperature" for sensor in catalog[0]["sensors"]))

    def test_bad_values_are_refused_with_danish_messages(self):
        for bad in (
            [room("")],
            [room("A", "garage")],
            [room("A", priority="max")],
            [room("A", sensors={"temperature": "light.lamp"})],
            [room("A", control="ja")],
            [room("A"), room("a")],
            [room("A", unit_sensor=True), room("B", unit_sensor=True)],
            [room("A", "bathroom", rh_setpoint=20)],
            [room(f"Rum {index}") for index in range(33)],
        ):
            with self.subTest(bad=bad), self.assertRaises(room_setup.RoomSetupError):
                room_setup.clean_rooms(bad)

    def test_excluded_room_never_controls_or_holds_the_unit_sensor(self):
        cleaned = room_setup.clean_room(room("Garage", "excluded", unit_sensor=True, control=True))
        self.assertFalse(cleaned["control"])
        self.assertFalse(cleaned["unit_sensor"])

    def test_guess_type(self):
        for name, expected in (("Garage", "excluded"), ("Garagen", "excluded"), ("Badeværelse", "bathroom"),
                               ("Lille Wc", "bathroom"), ("Køkkenet", "kitchen"), ("Soveværelse", "bedroom"),
                               ("Brændeovn", "source"), ("Udeluft", "outdoor"), ("Udenfor", "outdoor"),
                               ("Udsugningsrum", "extract"), ("Stue", "normal"), ("Mads", "normal"),
                               ("Indkørsel/fordør", "outdoor")):
            with self.subTest(name=name):
                self.assertEqual(room_setup.guess_type(name), expected)

    def test_catalog_is_cleaned(self):
        areas = room_setup.clean_catalog([
            {"area_id": "stue", "area_name": "Stue", "sensors": [
                {"entity_id": "sensor.ok", "kind": "temperature"},
                {"entity_id": "light.no", "kind": "temperature"},
                {"entity_id": "sensor.bad_kind", "kind": "voltage"},
            ]},
            {"area_id": "stue", "sensors": []},
        ])
        self.assertEqual(len(areas), 1)
        self.assertEqual([s["entity_id"] for s in areas[0]["sensors"]], ["sensor.ok"])
        with self.assertRaises(room_setup.RoomSetupError):
            room_setup.clean_catalog({"not": "a list"})


class RoomSetupConfigTests(unittest.TestCase):
    def make_runtime(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "controller.json"
        return ControllerRuntime(gateway_state={}, hardware=HardwareAdapter(), state_path=self.path)

    def send(self, runtime, rooms):
        runtime.room_inputs({"source": "home_assistant", "valid_for_s": 180, "rooms": rooms})

    def test_room_setup_is_stored_and_survives_a_restart(self):
        runtime = self.make_runtime()
        runtime.config.configure({"room_setup": [room("Stue", sensors={"temperature": "sensor.t"}), room("Garage", "excluded")]})
        self.assertTrue(runtime.config.data["room_setup_migrated"])
        snapshot = runtime.snapshot()
        self.assertNotIn("room_setup", snapshot)  # read from /api/controller/rooms instead
        self.assertEqual(snapshot["room_setup_count"], 2)
        again = ControllerState(self.path)
        self.assertEqual([r["name"] for r in again.data["room_setup"]], ["Stue", "Garage"])
        with self.assertRaises(ControllerError):
            runtime.config.configure({"room_setup": [room("A", "loft")]})
        # A refused list leaves the stored one alone.
        self.assertEqual(len(runtime.config.data["room_setup"]), 2)

    def test_excluded_room_is_ignored_everywhere(self):
        runtime = self.make_runtime()
        runtime.config.configure({"local_normal_level": 3, "room_setup": [room("Stue"), room("Garage", "excluded")]})
        self.send(runtime, {"Stue": {"temperature": 21.0, "humidity": 45}, "Garage": {"temperature": 4.0, "humidity": 90, "co2": 2500}})
        snapshot = runtime.snapshot()
        self.assertNotIn("Garage", snapshot["smart_ha_rooms"])
        self.assertNotIn("Garage", snapshot["smart_rooms"])
        self.assertNotEqual(runtime.smart_controlling_room, "Garage")
        self.assertEqual(runtime._ha_room_average_temperature(), 21.0)
        self.assertEqual(runtime._indoor_room_humidity()["indoor_rh_average"], 45.0)
        self.assertNotIn("Garage", snapshot["measurement_rooms"])

    def test_mapping_on_the_pi_wins_over_what_ha_sends(self):
        runtime = self.make_runtime()
        runtime.config.configure({"local_normal_level": 3, "co2_setpoint": 800, "room_setup": [
            room("Kælder", "bathroom", priority="high", rh_setpoint=70),
            room("Kontor", control=False),
        ]})
        self.send(runtime, {"kælder": {"humidity": 75, "room_type": "normal", "priority": "low"},
                            "Kontor": {"co2": 1600, "control": True}})
        rooms = runtime.snapshot()["smart_ha_rooms"]
        self.assertIn("Kælder", rooms)  # the name from the room setup
        self.assertEqual(rooms["Kælder"]["room_type"], "bathroom")
        self.assertEqual(rooms["Kælder"]["priority"], "high")
        self.assertFalse(rooms["Kontor"]["control"])
        self.assertIn("Kælder", runtime._bathroom_episodes)
        self.assertNotEqual(runtime.smart_controlling_room, "Kontor")

    def test_measuring_rooms_stay_out_of_indoor_averages(self):
        runtime = self.make_runtime()
        runtime.config.configure({"room_setup": [room("Stue"), room("Brændeovn", "source"), room("Udeluft", "outdoor"),
                                                 room("Udsugningsrum", "extract")]})
        self.send(runtime, {"Stue": {"temperature": 21.0, "humidity": 40}, "Brændeovn": {"temperature": 60.0},
                            "Udeluft": {"humidity": 95}, "Udsugningsrum": {"temperature": 23.0, "humidity": 50}})
        self.assertEqual(runtime._ha_room_average_temperature(), 21.0)
        self.assertEqual(runtime._indoor_room_humidity()["indoor_rh_average"], 40.0)
        self.assertIn("Brændeovn", runtime.snapshot()["measurement_rooms"])
        # Still usable as sources.
        runtime.config.configure({"outdoor_humidity_source": "room:Udeluft"})
        self.assertEqual(runtime._outdoor_humidity(), 95.0)

    def test_set_up_room_without_values_is_skipped_not_refused(self):
        runtime = self.make_runtime()
        runtime.config.configure({"room_setup": [room("Stue"), room("Mads")]})
        self.send(runtime, {"Stue": {"temperature": 21.0}, "Mads": {}})
        self.assertEqual(list(runtime.smart_rooms), ["Stue"])

    def test_occupancy_from_1_12_0_is_ignored(self):
        # A config saved by 1.12.0/1.12.1 with the occupancy settings and a presence sensor per room.
        self.make_runtime()
        self.path.write_text(json.dumps({
            "presence_away_enabled": True, "presence_away_level": 1, "presence_busy_enabled": True,
            "presence_busy_rooms": 2, "presence_on_minutes": 5, "presence_off_minutes": 15,
            "room_setup": [{"name": "Stue", "presence_auto": False,
                            "sensors": {"temperature": "sensor.t", "presence": "binary_sensor.radar"}}],
        }), encoding="utf-8")
        runtime = ControllerRuntime(gateway_state={}, hardware=HardwareAdapter(), state_path=self.path)
        self.assertFalse(any(key.startswith("presence_") for key in runtime.config.data))
        stue = runtime.config.data["room_setup"][0]
        self.assertEqual(stue["sensors"]["temperature"], "sensor.t")
        self.assertNotIn("presence", stue["sensors"])
        self.assertNotIn("presence_auto", stue)
        runtime.config.configure({"presence_away_enabled": True, "local_normal_level": 2})
        self.assertNotIn("presence_away_enabled", runtime.config.data)
        # Presence still sent by an older integration is skipped, never refused.
        self.send(runtime, {"Stue": {"temperature": 21.0, "presence": True}, "Kontor": {"presence": False}})
        self.assertEqual(list(runtime.smart_rooms), ["Stue"])
        self.assertNotIn("presence", runtime.smart_rooms["Stue"])
        snapshot = runtime.snapshot()
        for key in ("presence_state", "occupied_rooms", "home_state"):
            self.assertNotIn(key, snapshot)

    def test_unit_sensor_fills_its_room_instead_of_a_separate_room(self):
        runtime = self.make_runtime()
        runtime.gateway_state.update({"humidity": 44.0, "room_temp": 22.5})
        self.send(runtime, {"Stue": {"temperature": 21.0}})
        self.assertIn("HCH5 / lokale sensorer", runtime._rooms_with_unit_sensors())
        runtime.config.configure({"room_setup": [room("Stue"), room("Spisestue", unit_sensor=True)]})
        combined = runtime._rooms_with_unit_sensors()
        self.assertNotIn("HCH5 / lokale sensorer", combined)
        self.assertEqual(combined["Spisestue"]["humidity"], 44.0)
        self.assertEqual(combined["Spisestue"]["temperature"], 22.5)
        self.assertTrue(combined["Spisestue"]["unit_sensor"])
        # HA's own sensor in that room wins; the unit fills in the rest.
        self.send(runtime, {"Stue": {"temperature": 21.0}, "Spisestue": {"temperature": 20.0}})
        combined = runtime._rooms_with_unit_sensors()
        self.assertEqual(combined["Spisestue"]["temperature"], 20.0)
        self.assertEqual(combined["Spisestue"]["humidity"], 44.0)

    def test_legacy_rooms_without_setup_keep_working(self):
        runtime = self.make_runtime()
        self.send(runtime, {"Bad": {"humidity": 80}, "Stue": {"co2": 700}})
        self.assertEqual(sorted(runtime.smart_rooms), ["Bad", "Stue"])
        with self.assertRaises(ControllerError):
            self.send(runtime, {"Stue": {}})


class CatalogAndMigrationTests(RoomSetupConfigTests):
    LEGACY = {
        "Soveværelse": {"co2": 700, "entities": {"co2": "sensor.luftkvalitet_sovevaerelse_carbon_dioxide"}, "priority": "high"},
        "Badeværelse": {"humidity": 60, "entities": {"humidity": "sensor.bad_rh"}, "room_type": "bathroom"},
        "Brændeovn": {"temperature": 40, "entities": {"temperature": "sensor.braendeovn_temp_fugtighed_temperature"}},
        "Udeluft": {"humidity": 90, "entities": {"humidity": "sensor.ude_rh"}},
        "Hus": {"temperature": 21, "entities": {"temperature": "sensor.hus_t"}},
    }

    def test_catalog_is_kept_and_legacy_rooms_move_once(self):
        runtime = self.make_runtime()
        runtime.config.configure({"fireplace_auto_source": "room:Brændeovn"})
        view = runtime.ha_catalog_input({"areas": CATALOG, "legacy_rooms": self.LEGACY})
        self.assertTrue(view["migrated_now"])
        self.assertEqual(len(view["catalog"]), 3)
        rooms = {r["name"]: r for r in view["rooms"]}
        self.assertEqual(rooms["Soveværelse"]["type"], "bedroom")
        self.assertEqual(rooms["Soveværelse"]["area_id"], "sovevaerelse")
        self.assertEqual(rooms["Soveværelse"]["priority"], "high")
        self.assertEqual(rooms["Badeværelse"]["type"], "bathroom")
        # A stove sensor in the living room does not claim the living room area.
        self.assertEqual(rooms["Brændeovn"]["type"], "source")
        self.assertIsNone(rooms["Brændeovn"]["area_id"])
        self.assertEqual(rooms["Udeluft"]["type"], "outdoor")
        self.assertEqual(rooms["Hus"]["sensors"]["temperature"], "sensor.hus_t")
        self.assertTrue(runtime.config.data["room_setup_migrated"])
        # Never again, even after the user has emptied the list.
        runtime.config.configure({"room_setup": []})
        view = runtime.ha_catalog_input({"areas": CATALOG, "legacy_rooms": self.LEGACY})
        self.assertFalse(view["migrated_now"])
        self.assertEqual(view["rooms"], [])

    def test_existing_setup_is_never_overwritten(self):
        runtime = self.make_runtime()
        runtime.config.data["room_setup"] = [room_setup.clean_room(room("Stue"))]
        view = runtime.ha_catalog_input({"areas": CATALOG, "legacy_rooms": self.LEGACY})
        self.assertFalse(view["migrated_now"])
        self.assertEqual([r["name"] for r in view["rooms"]], ["Stue"])

    def test_bathroom_limits_from_the_webui_move_into_the_room(self):
        runtime = self.make_runtime()
        runtime.config.configure({"bathroom_rooms": {"Badeværelse": {"type": "bathroom", "rh_setpoint": 72, "max_level": 3}}})
        view = runtime.ha_catalog_input({"areas": CATALOG, "legacy_rooms": self.LEGACY})
        bath = next(r for r in view["rooms"] if r["name"] == "Badeværelse")
        self.assertEqual(bath["rh_setpoint"], 72.0)
        self.assertEqual(bath["max_level"], 3)

    def test_bad_catalog_is_refused(self):
        runtime = self.make_runtime()
        with self.assertRaises(ControllerError):
            runtime.ha_catalog_input({"areas": "Stue"})

    def test_rooms_view_lists_types_and_catalog_age(self):
        runtime = self.make_runtime()
        view = runtime.rooms_view()
        self.assertIsNone(view["catalog_age_seconds"])
        self.assertEqual([t["id"] for t in view["room_types"]], list(room_setup.ROOM_TYPES))
        runtime.ha_catalog_input({"areas": CATALOG})
        self.assertIsNotNone(runtime.rooms_view()["catalog_age_seconds"])
        self.assertIsNone(runtime.rooms_view(include_catalog=False)["catalog"])

    def test_api_key_scopes(self):
        self.assertEqual(ENDPOINT_SCOPES[("POST", "/api/controller/ha-catalog")], "control")
        self.assertEqual(ENDPOINT_SCOPES[("GET", "/api/controller/rooms")], "read")

    def test_change_log_keeps_a_short_summary(self):
        runtime = self.make_runtime()
        runtime.configure({"room_setup": [room("Stue"), room("Garage", "excluded")]}, apply=False, source="test")
        entry = runtime.change_log[-1]
        self.assertEqual(entry["changes"]["room_setup"], ["0 rum (0 aktive)", "2 rum (1 aktive)"])


if __name__ == "__main__":
    unittest.main()
