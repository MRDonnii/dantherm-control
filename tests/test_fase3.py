"""Phase 3 (1.13.0-beta.14): alarms and data you can trust. One small regression test per item."""
import json
import os
import sys
import tempfile
import time
import unittest
from collections import deque
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "gateway"))

from controller_core import ControllerState, HardwareAdapter  # noqa: E402
from controller_observability import ControllerObservability  # noqa: E402
from controller_runtime import ControllerRuntime  # noqa: E402
from dashboard_server import HistoryStore  # noqa: E402
from diagnostics import Diagnostics  # noqa: E402
from history_analysis import before_coil, comfort_report  # noqa: E402
from night_cooling import OutdoorHistory  # noqa: E402
from pi_health import PiHealth  # noqa: E402

T0 = datetime(2026, 1, 15, 12, 0).timestamp()


def base(**extra):
    s = {"outdoor_temperature": -2.0, "extract_temperature": 21.0, "exhaust_temperature": 2.0,
         "rs485_healthy": True, "effective_level": 3}
    s.update(extra)
    return s


def run(d, feed, start, minutes, step=10):
    """Feed ``minutes`` of identical data; returns the last result."""
    result = None
    for i in range(0, minutes * 60 + 1, step):
        result = d.update(dict(feed), start + i)
    return result


def codes(result):
    return [a["code"] for a in result["diagnostics_alarms"]]


class T2CorrectionTests(unittest.TestCase):
    def test_the_old_0_4_correction_is_raised_to_the_measured_1_5_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "controller.json"
            path.write_text(json.dumps({"t2_offset_k": 0.4}))
            config = ControllerState(path)
            self.assertEqual(config.data["t2_offset_k"], 1.5)
            self.assertTrue(config.data["t2_offset_calibrated"])
            config.data["t2_offset_k"] = 0.4  # the owner chooses 0.4 again: it is kept
            config.save()
            self.assertEqual(ControllerState(path).data["t2_offset_k"], 0.4)

    def test_another_own_value_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "controller.json"
            path.write_text(json.dumps({"t2_offset_k": 0.9}))
            self.assertEqual(ControllerState(path).data["t2_offset_k"], 0.9)

    def test_the_history_scale_uses_the_same_correction_and_ignores_glitches(self):
        self.assertEqual(before_coil({"afterheat_before_temperature": 17.0}, 1.5), (18.5, "measured"))
        # A 655 C T1 (old unsigned decode) is not a temperature: the estimate is not made from it.
        self.assertEqual(before_coil({"outdoor_temp": 655.3, "extract_temp": 20.0, "exhaust_temp": 4.0}), (None, None))

    def test_a_cold_coil_gives_a_suggestion_for_the_correction(self):
        d = Diagnostics()
        feed = base(afterheat_level=0, actual_afterheat=False, actual_supply_before_heater_temperature=17.0,
                    actual_supply_before_heater_estimate=18.5, heating_coil_after_temperature=18.4)
        result = run(d, feed, T0, 20 + 70)
        self.assertEqual(result["t2_offset_suggested_k"], 1.4)


class AlarmTests(unittest.TestCase):
    def test_coil_frost_below_10_degrees_is_critical_and_only_when_fresh(self):
        d = Diagnostics()
        self.assertNotIn("coil_frost", codes(run(d, base(coil_frost_temperature=15.2), T0, 5)))
        result = run(d, base(coil_frost_temperature=8.0), T0 + 400, 3)
        self.assertEqual([(a["code"], a["severity"]) for a in result["diagnostics_alarms"] if a["code"] == "coil_frost"],
                         [("coil_frost", "critical")])
        self.assertIn("8.0", result["diagnostics_alarm_text"])

    def test_cold_water_at_a_high_level_is_raised_after_15_minutes(self):
        d = Diagnostics()
        feed = base(afterheat_level=9, water_flow_temperature=30.0, water_return_temperature=14.0,
                    heating_coil_after_temperature=19.0)
        self.assertNotIn("afterheat_water_cold", codes(run(d, feed, T0, 10)))
        self.assertIn("afterheat_water_cold", codes(run(d, feed, T0 + 601, 6)))
        warm = base(afterheat_level=9, water_flow_temperature=46.0, water_return_temperature=30.0,
                    heating_coil_after_temperature=22.0)
        self.assertNotIn("afterheat_water_cold", codes(run(Diagnostics(), warm, T0, 40)))

    def test_no_lift_at_a_high_level_is_level_aware(self):
        d = Diagnostics()
        feed = base(afterheat_level=7, afterheat_lift=0.6, heating_coil_after_temperature=17.0,
                    afterheat_effective_setpoint=22)
        self.assertIn("afterheat_no_lift", codes(run(d, feed, T0, 25)))

    def test_a_room_sensor_that_is_stale_names_the_room(self):
        d = Diagnostics()
        feed = base(smart_inputs_online=True,
                    smart_ha_rooms={"Soveværelse": {"enabled": True, "control": True, "co2": 600.0}},
                    smart_room_quality={"Soveværelse": {"co2": {"state": "stale"}, "humidity": {"state": "unavailable"}}})
        result = run(d, feed, T0, 31)
        self.assertIn("room_sensor_missing", codes(result))
        self.assertIn("CO₂-måleren i Soveværelse", result["diagnostics_alarm_text"])
        # Offline HA has its own alarm; a room without control is not watched.
        quiet = dict(feed, smart_inputs_online=False)
        self.assertNotIn("room_sensor_missing", codes(run(Diagnostics(), quiet, T0, 31)))

    def test_a_room_left_out_by_ha_is_caught_but_source_rooms_are_not(self):
        feed = base(smart_inputs_online=True, smart_ha_rooms={},
                    smart_missing_sensors={"Børneværelse": ["co2"], "Brændeovn": ["humidity"]},
                    smart_source_rooms=["Brændeovn"])
        result = run(Diagnostics(), feed, T0, 31)
        self.assertIn("room_sensor_missing", codes(result))
        self.assertIn("Børneværelse", result["diagnostics_alarm_text"])
        self.assertNotIn("Brændeovn", result["diagnostics_alarm_text"])

    def test_room_humidity_that_stays_put_is_not_frozen(self):
        feed = base(smart_inputs_online=True,
                    smart_ha_rooms={"Stue": {"enabled": True, "control": True, "humidity": 40.0}},
                    smart_room_quality={"Stue": {"humidity": {"state": "fresh"}}})
        self.assertNotIn("Stue", str(run(Diagnostics(), feed, T0, 13 * 60 + 1, step=60).get("diagnostics_alarm_text")))

    def test_pilot_fallbacks_and_a_high_setpoint_rate_are_alarms(self):
        d = Diagnostics()
        result = d.update(base(afterheat_fallbacks_6h=3, afterheat_rate_warnings_1h=4), T0)
        self.assertIn("afterheat_pilot_fallback", codes(result))
        self.assertIn("afterheat_rate_high", codes(result))
        self.assertNotIn("afterheat_pilot_fallback", codes(Diagnostics().update(base(afterheat_fallbacks_6h=2), T0)))

    def test_fan_rpm_that_does_not_fit_the_percent_is_an_alarm(self):
        d = Diagnostics()
        good = base(actual_fan_extract_percent=70.0, actual_fan_extract_rpm=2235.0,
                    actual_fan_supply_percent=70.0, actual_fan_supply_rpm=2235.0)
        run(d, good, T0, 20)  # the reference is learned
        self.assertEqual(codes(d.update(dict(good), T0 + 1300)), [])
        bad = dict(good, actual_fan_extract_rpm=1700.0)
        self.assertIn("fan_speed_mismatch", codes(run(d, bad, T0 + 1400, 12)))
        # The supply fan is left alone while the unit's own frost protection cuts it.
        frost = dict(good, actual_fan_supply_rpm=1660.0, actual_unit_frost_protection=True)
        self.assertNotIn("fan_speed_mismatch", codes(run(Diagnostics(), frost, T0, 15)))

    def test_unit_that_does_not_follow_the_commanded_level(self):
        d = Diagnostics()
        feed = base(hardware_control_state="active_pi_master", effective_profile={"extract": 70, "supply": 70},
                    actual_fan_extract_percent=55.0)
        self.assertIn("level_not_followed", codes(run(d, feed, T0, 15)))
        ok = dict(feed, actual_fan_extract_percent=70.0)
        self.assertNotIn("level_not_followed", codes(run(Diagnostics(), ok, T0, 15)))
        # Not while a fireplace or the unit's own protection makes the difference.
        self.assertNotIn("level_not_followed", codes(run(Diagnostics(), dict(feed, fireplace=True), T0, 15)))

    def test_a_sensor_that_stands_still_for_hours_is_noted(self):
        d = Diagnostics()
        result = run(d, base(), T0, 4 * 60 + 1, step=60)
        self.assertIn("sensor_frozen", codes(result))
        self.assertEqual([a["severity"] for a in result["diagnostics_alarms"] if a["code"] == "sensor_frozen"], ["info"])
        moving = Diagnostics()
        for i in range(0, 5 * 3600, 60):
            drift = (i // 600) * 0.1
            result = moving.update(base(outdoor_temperature=-2.0 + drift, extract_temperature=21.0 + drift,
                                        exhaust_temperature=2.0 + drift), T0 + i)
        self.assertNotIn("sensor_frozen", codes(result))

    def test_the_pi_itself_disk_voltage_clock_and_history(self):
        d = Diagnostics()
        feed = base(pi_disk_used_percent=93.0, pi_undervoltage_now=True, pi_clock_synced=False,
                    history_recording_available=False)
        result = run(d, feed, T0, 31)
        for code in ("pi_disk_full", "pi_undervoltage", "history_paused", "clock_unsynced"):
            self.assertIn(code, codes(result))
        fine = base(pi_disk_used_percent=55.0, pi_undervoltage_now=False, pi_clock_synced=True,
                    history_recording_available=True)
        self.assertEqual(codes(run(Diagnostics(), fine, T0, 31)), [])

    def test_a_stale_afterheat_status_is_noted(self):
        self.assertIn("afterheat_status_stale", codes(run(Diagnostics(), base(afterheat_status_stale=True), T0, 3)))

    def test_the_heat_recovery_trend_needs_two_weeks_and_four_points(self):
        d = Diagnostics()
        for day in range(20):
            feed = base(outdoor_temperature=0.0, extract_temperature=21.0,
                        exhaust_temperature=21.0 - 21.0 * (0.92 if day < 13 else 0.86))
            start = T0 + day * 86400
            for i in range(0, 120 * 60, 60):
                result = d.update(dict(feed), start + i)
        self.assertEqual(result["recovery_median_7d"], 86.0)
        self.assertEqual(result["recovery_median_30d"], 92.0)
        self.assertIn("recovery_trend", codes(result))

    def test_an_alarm_holds_a_while_before_it_clears(self):
        d = Diagnostics()
        run(d, base(coil_frost_temperature=5.0), T0, 3)
        self.assertIn("coil_frost", codes(d.update(base(coil_frost_temperature=15.0), T0 + 200)))
        self.assertNotIn("coil_frost", codes(d.update(base(coil_frost_temperature=15.0), T0 + 200 + 301)))


class FilterReferenceTests(unittest.TestCase):
    def test_a_gear_pair_without_reference_learns_one_late(self):
        d = Diagnostics()
        feed = base(actual_fan_extract_percent=70.0, actual_fan_supply_percent=36.0, unit_power_w=60.0,
                    filter_hours_since_change=1288.0)
        result = run(d, feed, T0, 6)
        self.assertEqual(result["filter_reference_gears"], "70/36")
        self.assertEqual(result["filter_power_ratio"], 1.0)
        self.assertIn("efter filterskift", result["filter_reference_status"])
        self.assertEqual(d.filter_reference["70/36"]["h"], 1288.0)


class HistoryTests(unittest.TestCase):
    def test_glitch_values_are_not_stored_and_old_glitch_rows_are_cleaned(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "h.sqlite3"
            store = HistoryStore(path)
            store.record({"outdoor_temp": 655.33, "extract_temp": 21.0, "fan_level": 3, "level_source_code": 1,
                          "unit_frost_protection": True, "max_room_co2": 812.0}, now=1_000_060)
            row = store.rows(100, ("outdoor_temp", "extract_temp", "fan_level", "level_source_code",
                                   "unit_frost_protection", "max_room_co2"), now=1_000_100)[0]
            self.assertIsNone(row["outdoor_temp"])
            self.assertEqual((row["extract_temp"], row["fan_level"], row["level_source_code"],
                              row["unit_frost_protection"], row["max_room_co2"]), (21.0, 3.0, 1.0, 1.0, 812.0))
            with store._connect() as db:  # a row stored before the check
                db.execute("INSERT INTO samples (ts, outdoor_temp) VALUES (?, ?)", (1_000_200, 654.5))
            self.assertIsNone(store.rows(300, ("outdoor_temp",), now=1_000_300)[-1]["outdoor_temp"])
            reopened = HistoryStore(path)  # the file is cleaned when it is opened
            with reopened._connect() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM samples WHERE outdoor_temp > 100").fetchone()[0], 0)

    def test_the_weekly_comfort_numbers(self):
        rows = [{"ts": 1000 + i * 60, "max_room_co2": 1100 if i < 14 else 700, "indoor_rh_average": 28.0 if i < 60 else 40.0,
                 "unit_frost_protection": 1 if i < 5 else 0, "outdoor_temp": 0.0, "extract_temp": 21.0,
                 "exhaust_temp": 2.0} for i in range(120)]
        comfort = comfort_report(rows, 0)
        self.assertEqual((comfort["co2_over_1000_minutes"], comfort["dry_air_hours"], comfort["unit_frost_minutes"]),
                         (14, 1.0, 5))
        self.assertEqual(comfort["recovery_median_percent"], 90.5)
        self.assertTrue(comfort["lines"])
        # Rows from before the columns existed say nothing: not "no problem".
        self.assertIsNone(comfort_report([{"ts": 1000}], 0)["co2_over_1000_minutes"])


class RuntimeTests(unittest.TestCase):
    def make(self, state=None):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        os.environ["DANTHERM_CHANGE_LOG"] = str(Path(temp.name) / "change-log.jsonl")
        self.addCleanup(os.environ.pop, "DANTHERM_CHANGE_LOG", None)
        return ControllerRuntime(gateway_state=state or {}, hardware=HardwareAdapter(),
                                 state_path=Path(temp.name) / "controller.json")

    def test_frozen_afterheat_history_values_are_marked_unknown(self):
        runtime = self.make()
        runtime.gateway_state.update(afterheat_level=1, afterheat_level_target=1, afterheat_context=1)
        runtime._history_published_at = time.monotonic() - 10_000
        runtime._expire_history_values()
        self.assertIsNone(runtime.gateway_state["afterheat_level"])
        self.assertTrue(runtime.gateway_state["afterheat_status_stale"])
        runtime._publish_history_values({"afterheat_level": 2, "effective_level": 4, "effective_source": "ha_smart"})
        self.assertFalse(runtime.gateway_state["afterheat_status_stale"])
        self.assertEqual((runtime.gateway_state["fan_level"], runtime.gateway_state["level_source_code"]), (4, 1))

    def test_the_outdoor_season_is_rebuilt_from_the_history(self):
        history = OutdoorHistory()
        now = datetime(2026, 11, 25, 12, 0).timestamp()
        samples = [(now - 86400 * 2 + i * 60, 3.0) for i in range(0, 12 * 60)]  # 12 h two days ago
        samples += [(now - 3 * 3600 + i * 60, 5.0) for i in range(0, 3 * 60)]  # three hours today
        self.assertTrue(history.rebuild(samples, now))
        self.assertEqual([day[1] for day in history.days], [3.0])
        self.assertGreater(history.seconds, 2.9 * 3600)
        self.assertEqual(history.high, 5.0)
        self.assertFalse(OutdoorHistory().rebuild([], now))

    def test_the_condensation_check_gets_the_rooms_humidity_not_the_units(self):
        runtime = self.make({"outdoor_temp": 0.0, "extract_temp": 21.0, "exhaust_temp": 2.0, "humidity": 10.0})
        runtime.refresh_measurements()
        seen = []
        runtime.balance_learner.observe = lambda sample, now: seen.append(sample["rh"])
        runtime.engine.set_external({"indoor_rh_average": 31.0})
        runtime._observe_balance(1000.0)
        runtime.engine.set_external({})
        runtime._observe_balance(1010.0)
        self.assertEqual(seen, [31.0, None])

    def test_a_unit_rh_below_20_is_not_a_measurement(self):
        runtime = self.make({"humidity": 10.0, "bus_traffic": True, "bus_last_frame_age": 1})
        runtime.refresh_measurements()
        self.assertIsNone(runtime.engine.measurements.get("rh"))
        health = ControllerObservability(runtime)._data_health(runtime.snapshot())
        self.assertTrue(health["sensors"]["rh"].get("implausible"))
        self.assertFalse(health["sensors"]["rh"]["available"])

    def test_the_pilot_fallbacks_are_written_to_the_change_log(self):
        runtime = self.make()
        pilot = runtime.engine.afterheat_pilot
        pilot.fallback_times.append(time.time())
        pilot.fallback_reason = "HAC1 fulgte ikke Pi-styringen 3 gange i træk"
        runtime._note_pilot_events({})
        runtime._note_pilot_events({})
        self.assertEqual(sum(1 for e in runtime.change_log if "afterheat_fallback" in e["changes"]), 1)

    def test_cool_boost_stopped_by_the_frost_lock_is_logged(self):
        runtime = self.make()
        runtime.engine.cool_stopped_by_frost = True
        runtime._note_frost_lock()
        self.assertTrue(any(e.get("reason") == "Køl stoppet: frostsikring" for e in runtime.change_log))
        self.assertFalse(runtime.engine.cool_stopped_by_frost)


class PiHealthTests(unittest.TestCase):
    def test_values_are_read_once_a_minute_and_a_missing_tool_is_none(self):
        calls = []

        def runner(*args):
            calls.append(args[0])
            return {"/usr/bin/vcgencmd": "throttled=0x50005", "timedatectl": "no"}.get(args[0])

        health = PiHealth(runner=runner)
        first = health.read(100.0)
        self.assertTrue(first["pi_undervoltage_now"])
        self.assertIs(first["pi_clock_synced"], False)
        self.assertLessEqual(first["pi_disk_used_percent"], 100.0)
        health.read(130.0)
        self.assertEqual(len(calls), 2)  # cached
        self.assertIsNone(PiHealth(runner=lambda *a: None).read(1.0)["pi_undervoltage_now"])


class PilotStateTests(unittest.TestCase):
    def test_recent_counts_only_the_window(self):
        from afterheat_pilot import AfterheatPilot
        times = deque([0.0, 20000.0, 21000.0])
        self.assertEqual(AfterheatPilot._recent(times, 21500.0, 3600), 2)
        self.assertEqual(AfterheatPilot._recent(times, 21500.0, 6 * 3600), 3)


if __name__ == "__main__":
    unittest.main()
