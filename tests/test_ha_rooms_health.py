"""Combined HA-room status: connection and measurement age are separate (Change by Claude AI)."""
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "gateway"))
from controller_core import HardwareAdapter  # noqa: E402
from controller_observability import ControllerObservability  # noqa: E402
from controller_runtime import ControllerRuntime  # noqa: E402
from diagnostics import Diagnostics  # noqa: E402
from history_analysis import weekly_health  # noqa: E402
from room_quality import quality_rooms  # noqa: E402

FOUR_HOURS = 4 * 3600


def health(rooms, *, online=True, quality=True):
    snapshot = {"smart_inputs_online": online, "smart_ha_rooms": rooms}
    if quality:
        snapshot["smart_room_quality"] = quality_rooms(rooms, 0.0, online=online)[1]
    return ControllerObservability._ha_rooms_health(snapshot)


def strict(**values):
    return {"measurement_age_version": 1, **values}


class CombinedStatusTests(unittest.TestCase):
    def test_fresh_connection_with_an_old_measurement_is_not_fresh(self):
        item = health({"Stue": strict(co2=1800, co2_age_s=FOUR_HOURS)})
        self.assertTrue(item["connection_online"])
        self.assertEqual(item["state"], "stale")
        self.assertIs(item["fresh"], False)
        self.assertEqual((item["fresh_count"], item["stale_count"], item["unknown_age_count"]), (0, 1, 0))
        self.assertEqual(item["used_count"], 0)

    def test_mix_of_fresh_and_old_measurements_is_visible(self):
        item = health({
            "Stue": strict(temperature=21.5, temperature_age_s=60, co2=1800, co2_age_s=FOUR_HOURS),
            "Bad": strict(humidity=70, humidity_age_s=30),
        })
        self.assertEqual(item["state"], "mixed")
        self.assertIs(item["fresh"], False)
        self.assertEqual((item["fresh_count"], item["stale_count"], item["unknown_age_count"]), (2, 1, 0))
        self.assertEqual(item["measurements"], 3)
        self.assertEqual(item["used_count"], 2)

    def test_unknown_age_is_unknown_and_old_clients_stay_usable(self):
        item = health({"Old client": {"temperature": 21.0, "co2": 700}})
        self.assertEqual(item["state"], "unknown")
        self.assertIsNone(item["fresh"])
        self.assertEqual(item["unknown_age_count"], 2)
        # The existing filter still lets a client without age metadata regulate.
        self.assertEqual(item["used_count"], 2)
        strict_item = health({"Stue": strict(co2=700)})
        self.assertEqual(strict_item["state"], "unknown")
        self.assertEqual(strict_item["used_count"], 0)

    def test_fresh_and_unknown_age_together_are_unknown_not_fresh(self):
        item = health({"Stue": strict(temperature=21.0, temperature_age_s=10), "Old": {"co2": 600}})
        self.assertEqual(item["state"], "unknown")
        self.assertIsNone(item["fresh"])
        self.assertEqual((item["fresh_count"], item["unknown_age_count"]), (1, 1))

    def test_missing_optional_sensors_do_not_mark_the_rooms_faulty(self):
        item = health({
            "Stue": strict(temperature=21.0, temperature_age_s=10),
            "Kontor": strict(humidity=45, humidity_age_s=20),
        })
        self.assertEqual(item["state"], "fresh")
        self.assertIs(item["fresh"], True)
        self.assertTrue(item["available"])
        self.assertEqual((item["fresh_count"], item["stale_count"], item["unknown_age_count"]), (2, 0, 0))

    def test_zero_ha_rooms_are_not_fresh_measurements(self):
        item = health({})
        self.assertEqual(item["state"], "no_rooms")
        self.assertIsNot(item["fresh"], True)
        self.assertFalse(item["available"])
        self.assertTrue(item["connection_online"])
        empty = health({"Stue": strict()})
        self.assertEqual(empty["state"], "no_measurements")
        self.assertIsNot(empty["fresh"], True)
        self.assertFalse(empty["available"])

    def test_lost_connection_stays_a_separate_offline_state(self):
        item = health({"Stue": strict(co2=600, co2_age_s=5)}, online=False)
        self.assertEqual(item["state"], "offline")
        self.assertIs(item["fresh"], False)
        self.assertFalse(item["connection_online"])
        self.assertTrue(item["available"])
        self.assertEqual(item["fresh_count"], 0)
        self.assertEqual(item["measurements"], 1)

    def test_disabled_room_is_not_counted(self):
        item = health({
            "Stue": strict(temperature=21.0, temperature_age_s=10),
            "Gæst": strict(enabled=False, co2=900, co2_age_s=FOUR_HOURS),
        })
        self.assertEqual(item["state"], "fresh")
        self.assertEqual(item["rooms"], 1)
        self.assertEqual(item["value"], 2)

    def test_without_quality_in_the_snapshot_the_status_is_still_computed(self):
        item = health({"Stue": strict(co2=1800, co2_age_s=FOUR_HOURS)}, quality=False)
        self.assertEqual(item["state"], "stale")


class RuntimeTests(unittest.TestCase):
    def test_runtime_snapshot_reports_old_co2_resent_every_minute_as_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = ControllerRuntime(gateway_state={"extract_temp": 21, "outdoor_temp": 5},
                                        hardware=HardwareAdapter(), state_path=Path(tmp) / "state.json")
            runtime.room_inputs({"rooms": {"Stue": strict(temperature=21.5, temperature_age_s=30,
                                                          co2=1800, co2_age_s=FOUR_HOURS)},
                                 "valid_for_s": 180})
            runtime.smart_inputs_received_monotonic = time.monotonic()
            snapshot = runtime.snapshot()
            self.assertTrue(snapshot["smart_inputs_online"])
            item = ControllerObservability(runtime, path=Path(tmp) / "log")._data_health(snapshot)["sensors"]["ha_rooms"]
            self.assertEqual(item["state"], "mixed")
            self.assertIs(item["fresh"], False)
            self.assertTrue(item["connection_online"])
            self.assertEqual((item["fresh_count"], item["stale_count"]), (1, 1))
            # Regulation is unchanged: the old CO2 was already left out.
            self.assertIsNone(runtime._fresh_ha_rooms()["Stue"].get("co2"))
            self.assertEqual(runtime._fresh_ha_rooms()["Stue"].get("temperature"), 21.5)


class ConsumerTests(unittest.TestCase):
    def test_diagnostics_does_not_raise_a_local_alarm_for_old_ha_rooms(self):
        diag = Diagnostics()
        ha = health({"Stue": strict(co2=1800, co2_age_s=FOUR_HOURS)})
        local = {"available": True, "fresh": True, "value": 5.0}
        for minute in range(40):
            result = diag.update({"data_health": {"sensors": {"outdoor": local, "co2": dict(local, value=600),
                                                              "ha_rooms": ha}}}, 1000.0 + minute * 60)
        self.assertNotIn("local_sensor_stale", [a["code"] for a in result["diagnostics_alarms"]])

    def report(self, ha):
        return weekly_health([], events=[], restarts=[], now=2_000_000_000.0,
                             controller={"data_health": {"sensors": {"ha_rooms": ha}}})["stale"]

    def test_weekly_report_lists_mixed_rooms_but_not_unknown_age_or_zero_rooms(self):
        mixed = self.report(health({"Stue": strict(temperature=21, temperature_age_s=5, co2=1800, co2_age_s=FOUR_HOURS)}))
        self.assertEqual(mixed["now"], ["ha_rooms"])
        self.assertEqual(mixed["ha_rooms"]["state"], "mixed")
        self.assertEqual((mixed["ha_rooms"]["fresh_count"], mixed["ha_rooms"]["stale_count"]), (1, 1))
        self.assertEqual(self.report(health({"Old": {"co2": 600}}))["now"], [])
        self.assertEqual(self.report(health({}))["now"], [])

    def test_weekly_report_accepts_an_older_ha_rooms_entry(self):
        stale = self.report({"available": True, "fresh": True, "value": 2})
        self.assertEqual(stale["now"], [])
        self.assertIsNone(stale["ha_rooms"])


if __name__ == "__main__":
    unittest.main()
