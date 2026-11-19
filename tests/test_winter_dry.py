"""Winter dry-air mode: lower base level when it is cold outside and dry inside."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "gateway"))
from controller_core import ControllerEngine, ControllerError, ControllerState, HardwareAdapter  # noqa: E402
from controller_runtime import ControllerRuntime  # noqa: E402

T0 = 1_000_000.0
HOLD = 30 * 60


class WinterDryEngineTests(unittest.TestCase):
    def make_engine(self, **config):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        state = ControllerState(Path(temp.name) / "controller.json")
        state.configure({"winter_dry_enabled": True, **config})
        return ControllerEngine(state, HardwareAdapter())

    def test_cold_and_dry_holds_the_winter_base(self):
        engine = self.make_engine()
        engine.update_measurements(rh=33, co2=500, outdoor=0.2)
        result = engine.resolve(T0)
        self.assertTrue(result["winter_dry_active"])
        self.assertEqual(result["effective_level"], 2)
        self.assertEqual(result["effective_normal_level"], 2)
        self.assertEqual(result["effective_reason"], "Vinter: tør luft – holder trin 2 (inde 33 %, ude 0 °C)")

    def test_disabled_changes_nothing(self):
        engine = self.make_engine(winter_dry_enabled=False)
        engine.update_measurements(rh=33, co2=500, outdoor=0)
        result = engine.resolve(T0)
        self.assertFalse(result["winter_dry_active"])
        self.assertEqual(result["winter_dry_state"], "disabled")
        self.assertEqual(result["effective_level"], 3)

    def test_mild_weather_or_humid_house_changes_nothing(self):
        engine = self.make_engine()
        engine.update_measurements(rh=33, co2=500, outdoor=8)
        self.assertEqual(engine.resolve(T0)["effective_level"], 3)
        engine = self.make_engine()
        engine.update_measurements(rh=45, co2=500, outdoor=-5)
        self.assertEqual(engine.resolve(T0)["effective_level"], 3)

    def test_mild_co2_lifts_from_the_winter_base(self):
        engine = self.make_engine()
        engine.update_measurements(rh=33, co2=850, outdoor=0)
        result = engine.resolve(T0)
        self.assertTrue(result["winter_dry_active"])
        self.assertEqual(result["effective_level"], 3)
        self.assertIn("Vinter: tør luft – grundtrin 2", result["effective_reason"])
        self.assertIn("CO2 850 ppm", result["effective_reason"])

    def test_high_co2_pauses_winter_until_hold_time_after_it_clears(self):
        engine = self.make_engine()
        engine.update_measurements(rh=33, co2=500, outdoor=0)
        self.assertEqual(engine.resolve(T0)["effective_level"], 2)
        engine.update_measurements(co2=950)
        result = engine.resolve(T0 + 60)
        self.assertEqual(result["winter_dry_state"], "paused")
        self.assertEqual(result["effective_level"], 4)  # normal 3 + CO2
        self.assertIn("på pause: CO2 950 ppm", result["effective_reason"])
        # A room sensor counts too.
        engine.update_measurements(co2=500)
        engine.set_external({"max_room_co2": 1000})
        self.assertEqual(engine.resolve(T0 + 120)["winter_dry_state"], "paused")
        engine.set_external({})
        self.assertEqual(engine.resolve(T0 + 120 + HOLD - 1)["winter_dry_state"], "paused")
        result = engine.resolve(T0 + 120 + HOLD + 1)
        self.assertEqual(result["winter_dry_state"], "active")
        # It steps down through the normal downshift delay, not at once.
        self.assertEqual(result["effective_level"], 3)
        self.assertEqual(engine.resolve(T0 + 120 + HOLD + 400)["effective_level"], 2)

    def test_humid_extract_air_pauses_winter(self):
        engine = self.make_engine()
        # The HA rooms are dry, but a shower makes the extract air humid.
        engine.update_measurements(rh=62, co2=500, outdoor=0)
        engine.set_external({"indoor_rh_average": 33})
        result = engine.resolve(T0)
        self.assertEqual(result["winter_dry_state"], "paused")
        self.assertGreaterEqual(result["effective_level"], 4)
        self.assertIn("fugt 62 %", result["effective_reason"])

    def test_bathroom_drying_in_smart_auto_pauses_winter(self):
        engine = self.make_engine(mode="smart_auto")
        engine.update_measurements(rh=33, co2=500, outdoor=0)
        engine.config.heartbeat("boost", requested_level=4, valid_for_s=180,
                                reason="Badeværelse RH Bad 80%", bathroom_drying=True)
        result = engine.resolve(engine.config.data["ha_last_seen"])
        self.assertEqual(result["winter_dry_state"], "paused")
        self.assertEqual(result["effective_level"], 4)
        self.assertIn("badeværelse tørres ud", result["effective_reason"])

    def test_room_humidity_demand_in_smart_auto_pauses_winter(self):
        engine = self.make_engine(mode="smart_auto")
        engine.update_measurements(rh=33, co2=500, outdoor=0)
        engine.config.heartbeat("high", requested_level=4, valid_for_s=180,
                                reason="RH Køkken 60%", humidity_demand=True)
        result = engine.resolve(engine.config.data["ha_last_seen"])
        self.assertEqual(result["winter_dry_state"], "paused")
        self.assertEqual(result["effective_level"], 4)

    def test_smart_auto_never_goes_below_the_winter_base(self):
        engine = self.make_engine(mode="smart_auto")
        engine.update_measurements(rh=33, co2=450, outdoor=0)
        engine.config.heartbeat("low", requested_level=1, valid_for_s=180, reason="CO2 lav")
        result = engine.resolve(engine.config.data["ha_last_seen"])
        self.assertEqual(result["effective_level"], 2)
        self.assertIn("holder trin 2", result["effective_reason"])

    def test_hysteresis_and_hold_time(self):
        engine = self.make_engine()
        engine.update_measurements(rh=37, co2=500, outdoor=2)
        self.assertTrue(engine.resolve(T0)["winter_dry_active"])
        # Inside both hysteresis bands: stays on.
        engine.update_measurements(rh=40, outdoor=4.5)
        self.assertTrue(engine.resolve(T0 + 3 * HOLD)["winter_dry_active"])
        # Past the stop limit (38 + 3 %), but within the hold time: still on.
        engine = self.make_engine()
        engine.update_measurements(rh=37, co2=500, outdoor=2)
        engine.resolve(T0)
        engine.update_measurements(rh=42)
        self.assertTrue(engine.resolve(T0 + HOLD - 1)["winter_dry_active"])
        self.assertFalse(engine.resolve(T0 + HOLD + 1)["winter_dry_active"])
        # Back between the limits: does not start again (needs RH under 38 %).
        engine.update_measurements(rh=39)
        self.assertFalse(engine.resolve(T0 + 3 * HOLD)["winter_dry_active"])
        engine.update_measurements(rh=37.5)
        self.assertTrue(engine.resolve(T0 + 4 * HOLD)["winter_dry_active"])
        # Warm weather stops it: above 3 + 2 °C.
        engine.update_measurements(outdoor=5.5)
        self.assertFalse(engine.resolve(T0 + 6 * HOLD)["winter_dry_active"])

    def test_missing_sensor_stops_at_once(self):
        engine = self.make_engine()
        engine.update_measurements(rh=33, co2=500, outdoor=0)
        engine.resolve(T0)
        engine.measurements["outdoor"] = None
        result = engine.resolve(T0 + 60)
        self.assertEqual(result["winter_dry_state"], "sensor_missing")
        self.assertEqual(result["effective_level"], 3)

    def test_manual_boost_and_house_minimum_win(self):
        engine = self.make_engine(mode="manual", manual_level=4)
        engine.update_measurements(rh=33, co2=500, outdoor=0)
        self.assertEqual(engine.resolve(T0)["effective_level"], 4)

        engine = self.make_engine()
        engine.update_measurements(rh=33, co2=500, outdoor=0)
        engine.config.data["quick_boost_until"] = T0 + 600
        result = engine.resolve(T0)
        self.assertEqual(result["effective_level"], 4)
        self.assertEqual(result["effective_source"], "quick_boost")

        engine = self.make_engine(winter_level=1, local_min_level=2)
        engine.update_measurements(rh=33, co2=500, outdoor=0)
        self.assertEqual(engine.resolve(T0)["effective_level"], 2)

    def test_winter_level_at_or_above_base_does_nothing(self):
        engine = self.make_engine(winter_level=3)
        engine.update_measurements(rh=33, co2=500, outdoor=0)
        result = engine.resolve(T0)
        self.assertEqual(result["winter_dry_state"], "base_already_low")
        self.assertEqual(result["effective_reason"], "RH/CO2 normal")

    def test_lowest_room_basis(self):
        engine = self.make_engine(winter_rh_basis="lowest")
        engine.update_measurements(rh=45, co2=500, outdoor=0)
        engine.set_external({"indoor_rh_average": 40, "indoor_rh_lowest": 34})
        result = engine.resolve(T0)
        self.assertTrue(result["winter_dry_active"])
        self.assertEqual(result["winter_indoor_rh_source"], "ha_lowest")

    def test_settings_are_validated(self):
        engine = self.make_engine()
        with self.assertRaises(ControllerError):
            engine.config.configure({"winter_rh_basis": "median"})
        with self.assertRaises(ControllerError):
            engine.config.configure({"winter_level": 9})
        with self.assertRaises(ControllerError):
            engine.config.configure({"winter_outdoor_max": 40})
        engine.config.configure({"fan_step_count": 6})
        self.assertGreaterEqual(engine.config.data["winter_level"], 1)


class WinterDryRuntimeTests(unittest.TestCase):
    def test_room_humidity_leaves_out_bathrooms_and_uncontrolled_rooms(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        runtime = ControllerRuntime(gateway_state={}, hardware=HardwareAdapter(),
                                    state_path=Path(temp.name) / "controller.json")
        runtime.hardware_writes_allowed = lambda: False
        runtime.room_inputs({"source": "home_assistant", "valid_for_s": 180, "rooms": {
            "Stue": {"humidity": 37, "co2": 600},
            "Kontor": {"humidity": 33},
            "Badeværelse": {"humidity": 80},
            "Garage": {"humidity": 24, "control": False},
        }})
        runtime.refresh_measurements()
        self.assertEqual(runtime.engine.external["indoor_rh_average"], 35.0)
        self.assertEqual(runtime.engine.external["indoor_rh_lowest"], 33.0)


if __name__ == "__main__":
    unittest.main()
