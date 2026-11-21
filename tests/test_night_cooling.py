"""Night cooling from the DMI forecast (night_cooling and its use in the controller)."""
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "gateway"))

from controller_core import ControllerEngine, ControllerState, HardwareAdapter
from controller_runtime import ControllerRuntime
from night_cooling import NightCooling, OutdoorHistory, in_window

SETTINGS = {
    "night_cool_enabled": True, "night_cool_start": "23:00", "night_cool_end": "07:00",
    "night_cool_forecast_c": 24.0, "night_cool_today_c": 22.0, "night_cool_season_c": 12.0,
    "night_cool_delta_k": 2.0, "night_cool_min_indoor_c": 20.5,
}


def at(hour, minute=0, day=15):
    return datetime(2027, 7, day, hour, minute).astimezone()


def decide(cooling, local, **kwargs):
    values = {"indoor": 24.0, "outdoor": 15.0, "season_mean": 16.0, "forecast_high": 26.0,
              "today_high": 20.0, "blocked": None}
    values.update(kwargs)
    return cooling.update(now=local.timestamp(), local=local, settings=SETTINGS, **values)


class WindowTests(unittest.TestCase):
    def test_window_runs_past_midnight(self):
        self.assertTrue(in_window(at(23, 30), "23:00", "07:00"))
        self.assertTrue(in_window(at(3), "23:00", "07:00"))
        self.assertFalse(in_window(at(7), "23:00", "07:00"))
        self.assertFalse(in_window(at(14), "23:00", "07:00"))
        self.assertFalse(in_window(at(6), "00:30", "05:00"))


class NightCoolingRuleTests(unittest.TestCase):
    def test_starts_on_a_hot_forecast_with_reason(self):
        result = decide(NightCooling(), at(23, 30))
        self.assertTrue(result["night_cool_active"])
        self.assertEqual(result["night_cool_reason"], "Natkøling: varm dag i morgen (26 °C), ude 15 °C, inde 24,0 °C")

    def test_hot_day_behind_also_starts_it(self):
        result = decide(NightCooling(), at(23, 30), forecast_high=20.0, today_high=23.0)
        self.assertTrue(result["night_cool_active"])
        self.assertIn("varm dag i dag (23 °C)", result["night_cool_reason"])

    def test_no_hot_day_no_cooling(self):
        result = decide(NightCooling(), at(23, 30), forecast_high=21.0, today_high=19.0)
        self.assertFalse(result["night_cool_active"])
        self.assertEqual(result["night_cool_state"], "no_hot_day")

    def test_hot_day_stays_latched_after_midnight(self):
        cooling = NightCooling()
        self.assertTrue(decide(cooling, at(23, 30), forecast_high=20.0, today_high=23.0)["night_cool_active"])
        # After midnight "today's high" starts over and the forecast is mild: still cooling.
        self.assertTrue(decide(cooling, at(2, day=16), forecast_high=20.0, today_high=12.0)["night_cool_active"])

    def test_outside_season_or_window(self):
        self.assertEqual(decide(NightCooling(), at(23, 30), season_mean=11.0)["night_cool_state"], "not_season")
        self.assertEqual(decide(NightCooling(), at(23, 30), season_mean=None)["night_cool_state"], "no_season")
        self.assertEqual(decide(NightCooling(), at(21))["night_cool_state"], "outside_window")

    def test_needs_cooler_air_and_warm_rooms(self):
        self.assertEqual(decide(NightCooling(), at(23, 30), outdoor=22.5)["night_cool_state"], "not_cooler_outside")
        self.assertEqual(decide(NightCooling(), at(23, 30), indoor=20.4)["night_cool_state"], "indoor_satisfied")

    def test_stops_at_the_minimum_and_waits_before_restarting(self):
        cooling = NightCooling()
        self.assertTrue(decide(cooling, at(23, 30))["night_cool_active"])
        self.assertEqual(decide(cooling, at(1), indoor=20.5)["night_cool_state"], "indoor_satisfied")
        self.assertFalse(decide(cooling, at(1, 10), indoor=20.8)["night_cool_active"])  # needs +0.5 K
        self.assertTrue(decide(cooling, at(1, 20), indoor=21.1)["night_cool_active"])

    def test_stops_when_outdoor_is_no_longer_cooler_after_the_minimum_time(self):
        cooling = NightCooling()
        self.assertTrue(decide(cooling, at(23, 30))["night_cool_active"])
        self.assertTrue(decide(cooling, at(23, 35), outdoor=23.5)["night_cool_active"])  # minimum on time
        self.assertFalse(decide(cooling, at(23, 50), outdoor=23.5)["night_cool_active"])

    def test_yields_to_what_is_blocking(self):
        cooling = NightCooling()
        self.assertTrue(decide(cooling, at(23, 30))["night_cool_active"])
        result = decide(cooling, at(23, 40), blocked="pejs")
        self.assertFalse(result["night_cool_active"])
        self.assertEqual(result["night_cool_reason"], "Natkøling venter: pejs")

    def test_disabled(self):
        cooling = NightCooling()
        result = cooling.update(now=0, local=at(23, 30), settings={**SETTINGS, "night_cool_enabled": False},
                                indoor=24, outdoor=15, season_mean=16, forecast_high=26, today_high=20, blocked=None)
        self.assertEqual(result["night_cool_state"], "disabled")


class OutdoorHistoryTests(unittest.TestCase):
    def test_daily_means_and_running_mean(self):
        history = OutdoorHistory()
        start = at(0, 0, day=1).timestamp()
        completed = []
        for day in range(6):
            for step in range(24 * 6):  # every 10 minutes
                completed.append(history.add(10.0 + day, start + day * 86400 + step * 600))
        history.add(20.0, start + 6 * 86400)
        self.assertEqual(sum(completed) + 1, 6)
        self.assertEqual([day[1] for day in history.days], [11.0, 12.0, 13.0, 14.0, 15.0])
        self.assertEqual(history.running_mean(), 13.0)

    def test_short_day_does_not_count_and_clean_drops_bad_rows(self):
        history = OutdoorHistory([["2026-07-01", 14.0, 24], ["bad"], ["2026-07-02", 99, 24]])
        self.assertEqual(history.days, [["2026-07-01", 14.0, 24.0]])
        start = at(22, 0, day=2).timestamp()
        history.add(30.0, start)
        history.add(30.0, start + 600)
        self.assertEqual(history.running_mean(), 14.0)
        self.assertEqual(history.high, 30.0)


class NightCoolingEngineTests(unittest.TestCase):
    def make_engine(self, **settings):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        state = ControllerState(Path(temp.name) / "controller.json")
        state.configure({"night_cool_enabled": True, **settings})  # off by default since 1.12.3
        engine = ControllerEngine(state, HardwareAdapter())
        engine.outdoor_history.days = [["2026-07-1%d" % day, 17.0, 24.0] for day in range(5)]
        return engine

    def hot_night(self, engine, **extra):
        values = {"rooms_temperature": 24.0, "outdoor_air_temperature": 15.0, "outdoor_temperature": 15.0,
                  "forecast_high_c": 26.0, "today_high_c": 20.0, "afterheat_room_temperature": 24.0}
        values.update(extra)
        engine.set_external(values)
        engine.update_measurements(rh=45, co2=600, outdoor=15.0, room=24.0)

    def test_when_on_opens_bypass_raises_level_and_holds_afterheat_down(self):
        engine = self.make_engine()
        self.hot_night(engine)
        result = engine.resolve(at(23, 30).timestamp())
        self.assertTrue(result["night_cool_active"])
        self.assertEqual(result["effective_bypass"], "on")
        self.assertEqual(result["effective_level"], 4)
        self.assertEqual(result["effective_source"], "night_cooling")
        self.assertEqual(result["afterheat_effective_setpoint"], 10)
        self.assertEqual(result["afterheat_room_reason"], "Natkøling: eftervarme holdt nede")
        self.assertIn("Natkøling: varm dag i morgen (26 °C)", result["effective_reason"])
        self.assertEqual(result["night_cool_season_mean"], 17.0)

    def test_never_above_the_night_limit(self):
        engine = self.make_engine(night_enabled=True, night_start="22:00", night_end="06:00", night_level=2,
                                  night_air_quality_max_level=3)
        self.hot_night(engine)
        result = engine.resolve(at(23, 30).timestamp())
        self.assertTrue(result["night_cool_active"])
        self.assertEqual(result["effective_level"], 3)
        self.assertEqual(result["effective_bypass"], "on")

    def test_fireplace_vacation_and_frost_lock_win(self):
        for patch in ({"fireplace": True}, {"vacation_enabled": True, "vacation_until": "2027-07-20T12:00"}):
            with self.subTest(patch=patch):
                engine = self.make_engine(**patch)
                if "fireplace" in patch:
                    engine.config.data["fireplace_until"] = at(23, 59).timestamp()
                self.hot_night(engine)
                result = engine.resolve(at(23, 30).timestamp())
                self.assertFalse(result["night_cool_active"])
                self.assertEqual(result["night_cool_state"], "blocked")
        engine = self.make_engine()
        self.hot_night(engine)
        engine.frost_lock_active = True
        engine._update_frost_lock = lambda now: True
        self.assertIn("frostsikring", engine.resolve(at(23, 30).timestamp())["night_cool_reason"])

    def test_standby_wins(self):
        engine = self.make_engine(standby_minutes=-1)
        self.hot_night(engine)
        result = engine.resolve(at(23, 30).timestamp())
        self.assertFalse(result["night_cool_active"])
        self.assertEqual(result["effective_source"], "standby")


class ForecastSignalTests(unittest.TestCase):
    def test_forecast_highs_are_leased_signals(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        runtime = ControllerRuntime(gateway_state={}, hardware=HardwareAdapter(),
                                    state_path=Path(temp.name) / "controller.json")
        runtime.hardware_writes_allowed = lambda: False
        # home/home_reason from an older integration (0.9.0-beta.11) are ignored, not refused.
        result = runtime.external_signals({"valid_for_s": 300, "forecast_high_c": 26.4, "today_high_c": 21.0,
                                           "home": False, "home_reason": "Alarm: armed_away"})
        self.assertEqual(result["night_cool_forecast_high"], 26.4)
        self.assertEqual(result["night_cool_today_high"], 21.0)
        self.assertNotIn("home_state", result)
        self.assertNotIn("home_reason", result)
        runtime.climate_signals_until = {key: 0.0 for key in runtime.climate_signals_until}
        result = runtime.snapshot()
        self.assertIsNone(result["night_cool_forecast_high"])
        runtime.external_signals({"valid_for_s": 300, "home": "yes"})
        with self.assertRaises(Exception):
            runtime.external_signals({"valid_for_s": 300, "forecast_high_c": "varmt"})


if __name__ == "__main__":
    unittest.main()


class NightCoolingDefaultTests(unittest.TestCase):
    def test_off_by_default(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        state = ControllerState(Path(temp.name) / "controller.json")
        self.assertFalse(state.data["night_cool_enabled"])
