"""Review findings on 1.13.0-beta.2 (Change by Claude AI)."""
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "gateway"))
from control_history import ControlHistory  # noqa: E402
from diagnostics import OUTDOOR_EXPECTED_AFTER_SECONDS, Diagnostics  # noqa: E402
from history_analysis import weekly_health  # noqa: E402

try:
    import dantherm_gateway  # noqa: E402
except Exception:  # noqa: BLE001 - optional runtime dependencies
    dantherm_gateway = None

NOW = 2_000_000_000.0


def feed(outdoor=None, co2=None):
    def item(value):
        return {"available": value is not None, "fresh": value is not None, "value": value}
    return {"data_health": {"sensors": {"outdoor": item(outdoor), "co2": item(co2)}}}


def codes(result):
    return [alarm["code"] for alarm in result["diagnostics_alarms"]]


class WatchdogOrderTests(unittest.TestCase):
    @unittest.skipIf(dantherm_gateway is None, "gateway dependencies not installed")
    def test_hard_exit_is_armed_before_the_restart_log_is_written(self):
        gateway = dantherm_gateway.Gateway.__new__(dantherm_gateway.Gateway)
        gateway.controller = types.SimpleNamespace(config=types.SimpleNamespace(path="/nonexistent/controller.json"))
        order = []

        def hung_disk(*_args, **_kwargs):
            order.append("log")
            raise OSError("disk hangs")

        with patch.object(dantherm_gateway.Gateway, "arm_hard_exit", lambda self, seconds=None: order.append("arm")), \
                patch.object(dantherm_gateway, "record_watchdog_restart", hung_disk):
            error = gateway.watchdog_exit("stalled")
        self.assertEqual(order, ["arm", "log"])
        self.assertIsInstance(error, RuntimeError)


class MissingOutdoorTests(unittest.TestCase):
    def test_t1_missing_from_start_alarms_after_the_grace(self):
        diag = Diagnostics()
        result = {}
        for minute in range(0, 19):
            result = diag.update(feed(outdoor=None, co2=None), 1000.0 + minute * 60)
        self.assertIn("local_sensor_stale", codes(result))

    def test_no_alarm_during_the_start_grace(self):
        diag = Diagnostics()
        for minute in range(0, int(OUTDOOR_EXPECTED_AFTER_SECONDS // 60) - 1):
            result = diag.update(feed(outdoor=None, co2=None), 1000.0 + minute * 60)
        self.assertNotIn("local_sensor_stale", codes(result))

    def test_a_co2_sensor_never_seen_still_does_not_alarm(self):
        diag = Diagnostics()
        for minute in range(0, 30):
            result = diag.update(feed(outdoor=4.0, co2=None), 1000.0 + minute * 60)
        self.assertNotIn("local_sensor_stale", codes(result))


class WeeklyGapTests(unittest.TestCase):
    @staticmethod
    def rows(end, count=30, error=0.5):
        return [{"ts": end - (count - 1 - i) * 60, "heating_coil_after_temperature": 22.0 - error,
                 "afterheat_target": 22.0, "afterheat_active": 1, "afterheat_level": 1,
                 "fan_supply_percent": 36, "afterheat_context": 0} for i in range(count)]

    def test_an_open_gap_up_to_now_is_counted(self):
        report = weekly_health(self.rows(NOW - 90 * 60), events=[], restarts=[], controller={}, now=NOW)
        self.assertEqual(report["total"]["gap_minutes"], 90)
        self.assertEqual(report["open_gap_minutes"], 90)
        self.assertEqual(report["last_sample_age_minutes"], 90.0)
        self.assertEqual(sum(day["gap_minutes"] for day in report["per_day"]), 90)

    def test_a_recent_sample_is_no_gap(self):
        report = weekly_health(self.rows(NOW - 30), events=[], restarts=[], controller={}, now=NOW)
        self.assertEqual(report["total"]["gap_minutes"], 0)
        self.assertEqual(report["open_gap_minutes"], 0)

    def test_overshoot_and_undershoot_are_never_negative(self):
        total = weekly_health(self.rows(NOW, error=2.0), events=[], restarts=[], controller={}, now=NOW)["total"]
        self.assertEqual(total["max_over"], 0.0)
        self.assertEqual(total["max_under"], 2.0)


class RoomRecordingTableTests(unittest.TestCase):
    def test_a_deleted_database_file_gets_its_table_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "inputs.sqlite3"
            store = ControlHistory(path)
            store.record({"rooms": {"a": 1}}, now=10_000)
            path.unlink()
            store.record({"rooms": {"a": 2}}, now=10_120)  # fails: SQLite creates an empty file
            self.assertFalse(store.available)
            store.record({"rooms": {"a": 3}}, now=10_120 + 3600)
            self.assertTrue(store.available)
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM observations").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
