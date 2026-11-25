"""Dynamic dry-air protection (1.13.0-beta.16): RH tiers, absolute-humidity gate, priorities."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "gateway"))
from advanced_control import absolute_humidity, dry_air_stats  # noqa: E402
from controller_core import ControllerEngine, ControllerError, ControllerState, HardwareAdapter  # noqa: E402
from history_analysis import comfort_report  # noqa: E402

T0 = 1_000_000.0
HOLD = 10 * 60


class MagnusTests(unittest.TestCase):
    def test_known_values(self):
        # Saturation water content: 4.85 g/m3 at 0 C, 17.3 at 20 C; 50 % at 20 C = 8.65.
        self.assertAlmostEqual(absolute_humidity(0.0, 100.0), 4.85, delta=0.03)
        self.assertAlmostEqual(absolute_humidity(20.0, 100.0), 17.3, delta=0.1)
        self.assertAlmostEqual(absolute_humidity(20.0, 50.0), 8.65, delta=0.05)
        self.assertAlmostEqual(absolute_humidity(-10.0, 100.0), 2.36, delta=0.03)  # over water
        self.assertIsNone(absolute_humidity(None, 50.0))


class DryDynamicTests(unittest.TestCase):
    def make_engine(self, **config):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        state = ControllerState(Path(temp.name) / "controller.json")
        state.configure({"dry_protection_enabled": True, "dry_dynamic_enabled": True,
                         "local_min_level": 1, "dry_min_level": 1, "local_normal_level": 4, **config})
        return ControllerEngine(state, HardwareAdapter())

    def feed(self, engine, rh, *, t_in=22.0, t_out=2.0, rh_out=85.0, co2=500, unit_rh=None, room_co2=None):
        engine.update_measurements(rh=unit_rh, co2=co2, outdoor=t_out)
        engine.set_external({"indoor_rh_average": rh, "indoor_rh_lowest": rh, "indoor_temperature_average": t_in,
                             "dry_outdoor_temperature": t_out, "dry_outdoor_rh": rh_out,
                             "dry_outdoor_source": "udeluft", "max_room_co2": room_co2})

    def step(self, engine, now, rh, **kw):
        self.feed(engine, rh, **kw)
        return engine.resolve(now)

    def test_default_off_keeps_old_behaviour(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        state = ControllerState(Path(temp.name) / "controller.json")
        state.configure({"dry_protection_enabled": True, "winter_dry_enabled": True})
        data = state.data
        self.assertFalse(data["dry_dynamic_enabled"])
        self.assertFalse(data["dry_co2_soft_enabled"])
        self.assertEqual((data["dry_tier1_rh"], data["dry_tier1_level"], data["dry_tier2_rh"], data["dry_tier2_level"],
                          data["dry_dynamic_rh_hysteresis"], data["dry_min_level"], data["dry_co2_release_ppm"]),
                         (32.0, 2, 36.0, 3, 2.5, 2, 1200))
        engine = ControllerEngine(state, HardwareAdapter())
        result = self.step(engine, T0, 34.0, t_out=0.0)
        # The old modes run as before: winter mode holds its base, the plain limit waits at 34 % (> 30 %).
        self.assertEqual(result["winter_dry_state"], "active")
        self.assertEqual(result["effective_level"], 2)
        self.assertFalse(result["dry_dynamic_enabled"])
        self.assertIsNone(result["dry_tier"])
        self.assertIsNone(result["dry_reason"])

    def test_tiers_and_hysteresis(self):
        engine = self.make_engine(dry_hold_minutes=1)
        result = self.step(engine, T0, 30.0)
        self.assertEqual((result["dry_tier"], result["effective_level"]), (1, 2))
        self.assertEqual(result["effective_source"], "dry_protection")
        self.assertEqual(result["dry_uncapped_level"], 4)
        # 33 % is above 32 but below 32 + 2.5: still tier 1.
        self.assertEqual(self.step(engine, T0 + 120, 33.0)["dry_tier"], 1)
        result = self.step(engine, T0 + 240, 34.6)
        self.assertEqual((result["dry_tier"], result["effective_level"]), (2, 3))
        # 37 % is above 36 but within the hysteresis: still tier 2; 38.5 ends it.
        self.assertEqual(self.step(engine, T0 + 360, 37.0)["dry_tier"], 2)
        result = self.step(engine, T0 + 480, 38.5)
        self.assertEqual((result["dry_tier"], result["effective_level"]), (0, 4))
        self.assertEqual(result["dry_protection_state"], "standby")
        # Coming back from no tier: 36 % is not under 36, 35.9 is.
        self.assertEqual(self.step(engine, T0 + 600, 36.0)["dry_tier"], 0)
        self.assertEqual(self.step(engine, T0 + 720, 35.9)["dry_tier"], 2)

    def test_absolute_humidity_gate_with_hysteresis(self):
        engine = self.make_engine(dry_hold_minutes=1)
        # 22 C / 30 % = 5.83 g/m3 inside. Outdoor 6 C / 75 % = 5.4: 0.4 under the 0.5 margin.
        result = self.step(engine, T0, 30.0, t_out=6.0, rh_out=75.0)
        self.assertEqual(result["dry_protection_state"], "not_drying")
        self.assertFalse(result["dry_ah_drying"])
        self.assertEqual(result["effective_level"], 4)
        # Drier outside (2 C / 85 % = 4.7): on.
        result = self.step(engine, T0 + 120, 30.0)
        self.assertTrue(result["dry_ah_drying"])
        self.assertEqual(result["dry_tier"], 1)
        # Difference 0.4 (above margin - hysteresis = 0.2): stays on.
        self.assertTrue(self.step(engine, T0 + 240, 30.0, t_out=6.0, rh_out=75.0)["dry_ah_drying"])
        # Outdoor air as wet as inside: off, and the tier ends after the hold.
        result = self.step(engine, T0 + 360, 30.0, t_out=22.0, rh_out=30.0)
        self.assertFalse(result["dry_ah_drying"])
        self.assertEqual(result["dry_tier"], 0)
        self.assertEqual(result["effective_level"], 4)

    def test_missing_or_stale_data_switches_it_off_at_once(self):
        for missing in ("indoor_rh_average", "indoor_temperature_average", "dry_outdoor_temperature", "dry_outdoor_rh"):
            with self.subTest(missing=missing):
                engine = self.make_engine(dry_hold_minutes=60)
                self.assertEqual(self.step(engine, T0, 30.0)["dry_tier"], 1)
                self.feed(engine, 30.0)
                engine.external[missing] = None
                result = engine.resolve(T0 + 5)
                self.assertEqual(result["dry_protection_state"], "sensor_missing")
                self.assertEqual(result["dry_tier"], 0)
                self.assertEqual(result["effective_level"], 4)
                self.assertIn("mangler friske målinger", result["dry_reason"])

    def test_co2_over_setpoint_releases_at_once_and_restarts_under_hysteresis(self):
        engine = self.make_engine(dry_hold_minutes=60)
        self.assertEqual(self.step(engine, T0, 30.0)["dry_tier"], 1)
        result = self.step(engine, T0 + 5, 30.0, room_co2=850)
        self.assertEqual(result["dry_tier"], 0)
        self.assertEqual(result["dry_protection_state"], "co2_high")
        # 750 ppm is under the setpoint but over setpoint - hysteresis: waits.
        result = self.step(engine, T0 + 2 * HOLD * 6, 30.0, room_co2=750)
        self.assertEqual(result["dry_protection_state"], "co2_wait")
        self.assertEqual(self.step(engine, T0 + 2 * HOLD * 7, 30.0, room_co2=690)["dry_tier"], 1)

    def test_unit_and_room_humidity_demand_win(self):
        engine = self.make_engine(dry_hold_minutes=60)
        self.assertEqual(self.step(engine, T0, 30.0)["dry_tier"], 1)
        result = self.step(engine, T0 + 5, 30.0, unit_rh=70.0)  # the unit's own humidity control
        self.assertEqual(result["dry_tier"], 0)
        self.assertEqual(result["dry_protection_state"], "humidity_demand")
        self.assertIn("anlæggets egen fugtstyring", result["dry_reason"])

    def test_bathroom_drying_wins_in_smart_auto(self):
        engine = self.make_engine(mode="smart_auto", dry_hold_minutes=60)
        self.feed(engine, 30.0)
        engine.config.heartbeat("normal", requested_level=4, valid_for_s=180, reason="ok")
        seen = engine.config.data["ha_last_seen"]
        self.assertEqual(engine.resolve(seen)["effective_level"], 2)
        engine.config.heartbeat("boost", requested_level=5, valid_for_s=180, reason="Bad 80%", bathroom_drying=True)
        result = engine.resolve(engine.config.data["ha_last_seen"])
        self.assertGreaterEqual(result["effective_level"], 4)
        self.assertEqual(result["dry_protection_state"], "humidity_demand")

    def test_room_humidity_demand_wins_in_smart_auto(self):
        engine = self.make_engine(mode="smart_auto", dry_hold_minutes=60)
        self.feed(engine, 30.0)
        engine.config.heartbeat("high", requested_level=4, valid_for_s=180, reason="RH Køkken 60%",
                                humidity_demand=True)
        result = engine.resolve(engine.config.data["ha_last_seen"])
        self.assertEqual(result["effective_level"], 4)
        self.assertEqual(result["dry_tier"], 0)

    def test_floor_never_under_house_minimum(self):
        engine = self.make_engine(local_min_level=2, dry_tier1_level=1, dry_tier2_level=2)
        result = self.step(engine, T0, 28.0)
        self.assertEqual(result["dry_tier"], 1)
        self.assertEqual(result["dry_cap_level"], 2)
        self.assertEqual(result["effective_level"], 2)
        self.assertTrue(result["dry_floor_limited"])
        self.assertIn("hævet til husets mindste ventilation, trin 2", result["dry_reason"])
        engine = self.make_engine(local_min_level=1, dry_min_level=3, dry_tier1_level=1)
        self.assertEqual(self.step(engine, T0, 28.0)["effective_level"], 3)

    def test_hold_between_tier_changes(self):
        engine = self.make_engine(dry_hold_minutes=10)
        self.assertEqual(self.step(engine, T0, 30.0)["dry_tier"], 1)
        result = self.step(engine, T0 + 60, 40.0)
        self.assertEqual(result["dry_tier"], 1)  # dry enough again, but the hold runs
        self.assertEqual(result["dry_protection_state"], "held")
        self.assertEqual(self.step(engine, T0 + HOLD, 40.0)["dry_tier"], 0)

    def test_soft_co2_lifts_one_step_and_releases_over_its_limit(self):
        engine = self.make_engine(dry_co2_soft_enabled=True, dry_hold_minutes=60)
        self.assertEqual(self.step(engine, T0, 30.0)["effective_level"], 2)
        result = self.step(engine, T0 + 5, 30.0, room_co2=950)
        self.assertEqual((result["dry_tier"], result["dry_cap_level"]), (1, 3))
        self.assertTrue(result["dry_co2_lift"])
        self.assertIn("løfter ét trin", result["dry_reason"])
        # 850 keeps the lift (not yet at setpoint - hysteresis = 700).
        self.assertEqual(self.step(engine, T0 + 10, 30.0, room_co2=850)["dry_cap_level"], 3)
        self.assertEqual(self.step(engine, T0 + 12, 30.0, room_co2=1150)["dry_cap_level"], 3)
        result = self.step(engine, T0 + 15, 30.0, room_co2=1250)
        self.assertEqual(result["dry_tier"], 0)
        self.assertEqual(result["dry_protection_state"], "co2_high")
        # Restarts only at 1200 - 100 = 1100 ppm.
        later = T0 + 15 + 3600  # after the hold
        self.assertEqual(self.step(engine, later, 30.0, room_co2=1150)["dry_tier"], 0)
        self.assertEqual(self.step(engine, later + 5, 30.0, room_co2=1080)["dry_tier"], 1)
        self.assertEqual(self.step(engine, later + 10, 30.0, room_co2=650)["dry_cap_level"], 2)

    def test_winter_mode_steps_aside(self):
        engine = self.make_engine(winter_dry_enabled=True)
        result = self.step(engine, T0, 37.0, t_out=0.0)
        self.assertEqual(result["winter_dry_state"], "dynamic")
        self.assertEqual(result["effective_level"], 4)

    def test_settings_are_validated(self):
        engine = self.make_engine()
        with self.assertRaises(ControllerError):
            engine.config.configure({"dry_tier1_rh": 36.0, "dry_tier2_rh": 35.0})
        with self.assertRaises(ControllerError):
            engine.config.configure({"dry_tier1_level": 4, "dry_tier2_level": 3})


class DryStatsTests(unittest.TestCase):
    def test_moisture_and_heat(self):
        controller = {"dry_dynamic_enabled": True, "dry_ah_difference": 2.0, "recovery_median_7d": 90.0,
                      "dry_indoor_temperature": 22.0, "dry_outdoor_temperature": 2.0, "afterheat_target": 22.0,
                      "actual_afterheat": True, "heat_price_dkk_kwh": 0.6}
        stats = dry_air_stats(controller, airflow_m3h=150.0, airflow_without_m3h=220.0)
        self.assertEqual(stats["dry_moisture_loss_l_day"], 7.2)   # 150 * 2 * 24 / 1000
        self.assertEqual(stats["dry_moisture_kept_l_day"], 3.4)   # 70 * 2 * 24 / 1000
        # 70 m3/h * 1206 / 3600 * (0.1 * 20 K) = 46.9 W
        self.assertEqual(stats["dry_heat_saved_w"], 47)
        self.assertEqual(stats["dry_heat_saved_afterheat_w"], 47)
        self.assertEqual(stats["dry_heat_saved_kwh_day"], 1.13)
        self.assertEqual(stats["dry_stats_method"], "skøn")

    def test_off_gives_nothing(self):
        stats = dry_air_stats({"dry_dynamic_enabled": False}, airflow_m3h=150.0, airflow_without_m3h=None)
        self.assertIsNone(stats["dry_moisture_loss_l_day"])

    def test_history_hours(self):
        rows = [{"ts": i, "dry_limit_active": 1, "dry_tier": 1 if i < 60 else 2} for i in range(120)]
        report = comfort_report(rows, 0)
        self.assertEqual((report["dry_limit_hours"], report["dry_tier1_hours"], report["dry_tier2_hours"]), (2.0, 1.0, 1.0))


if __name__ == "__main__":
    unittest.main()
