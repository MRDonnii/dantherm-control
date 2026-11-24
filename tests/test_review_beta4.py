"""Review fixes in 1.13.0-beta.5 (Change by Claude AI).

1. The minute history resumes by itself after a failed write; the weekly report says
   when its data basis is missing or recording is paused.
2. A selected HA sensor without data (``missing_sensors`` or a set-up room HA left out)
   keeps the combined HA-room status from reading fresh.
4. Recent command errors and watchdog restarts are the newest ten, newest first.
5. A gap over midnight is split over the two days.
6. The afterheat precision in the weekly report can be filtered by operating mode.
"""
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "gateway"))
sys.path.insert(0, str(Path(__file__).parent))
from controller_core import HardwareAdapter  # noqa: E402
from controller_observability import ControllerObservability  # noqa: E402
from controller_runtime import ControllerRuntime  # noqa: E402
from dashboard_server import HISTORY_RETRY_FIRST_SECONDS, HistoryStore  # noqa: E402
from history_analysis import HEALTH_FIELDS, weekly_health  # noqa: E402
from room_quality import quality_rooms  # noqa: E402
from test_beta2_features import ServerFixture  # noqa: E402

NOW = 2_000_000_000.0


def sample(**extra):
    return {"heating_coil_after_temperature": 22.0, "afterheat_target": 22.0, "afterheat_active": 1, **extra}


class HistoryResumeTests(unittest.TestCase):
    def test_a_failed_write_pauses_and_resumes_by_itself_with_backoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.sqlite3"
            store = HistoryStore(path)
            store.record(sample(), now=NOW)
            # The disk "breaks": the database path is a directory, so every write fails.
            path.unlink()
            path.mkdir()
            with self.assertLogs("passivelink-dashboard", "WARNING") as logs:
                store.record(sample(), now=NOW + 60)
                self.assertFalse(store.available)
                status = store.status()
                self.assertEqual(status["state"], "retrying")
                self.assertEqual(status["failures"], 1)
                self.assertEqual(status["next_retry_at"], NOW + 60 + HISTORY_RETRY_FIRST_SECONDS)
                store.record(sample(), now=NOW + 90)  # before the retry: not even tried
                self.assertEqual(store.failures, 1)
                store.record(sample(), now=NOW + 60 + HISTORY_RETRY_FIRST_SECONDS)  # tried, fails again
                self.assertEqual(store.failures, 2)
                self.assertEqual(store.retry_at, NOW + 120 + 2 * HISTORY_RETRY_FIRST_SECONDS)
                # The disk works again: the next due retry recreates the table and records.
                path.rmdir()
                store.record(sample(), now=NOW + 3600)
            self.assertTrue(store.available)
            self.assertEqual(store.status()["recovered_at"], NOW + 3600)
            self.assertEqual(store.failures, 0)
            # Only the first failure and the recovery are logged as warnings.
            self.assertEqual(len(logs.records), 2, [r.getMessage() for r in logs.records])
            self.assertIn("paused", logs.records[0].getMessage())
            self.assertIn("resumed", logs.records[1].getMessage())
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM samples").fetchone()[0], 1)

    def test_the_backoff_is_capped_at_thirty_minutes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.sqlite3"
            path.mkdir()
            store = HistoryStore(path)
            self.assertFalse(store.available)
            now = NOW
            for _ in range(10):
                now = store.retry_at
                store.record(sample(), now=now)
            self.assertEqual(store.retry_at - now, 30 * 60)

    def test_a_deleted_file_gets_its_table_back_at_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.sqlite3"
            store = HistoryStore(path)
            store.record(sample(), now=NOW)
            path.unlink()
            store.record(sample(), now=NOW + 60)
            self.assertTrue(store.available)
            self.assertEqual(len(store.rows(3600, HEALTH_FIELDS, now=NOW + 60)), 1)

    def test_stored_rows_stay_readable_while_recording_is_paused(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = HistoryStore(Path(tmp) / "history.sqlite3")
            store.record(sample(), now=NOW)
            store.available, store.retry_at = False, NOW + 600  # paused
            self.assertEqual(len(store.rows(3600, HEALTH_FIELDS, now=NOW + 60)), 1)
            self.assertEqual(store.status()["state"], "retrying")

    def test_the_weekly_report_names_a_missing_or_paused_data_basis(self):
        empty = weekly_health([], events=[], restarts=[], controller={}, now=NOW,
                              history={"available": True, "state": "recording"})
        self.assertEqual(empty["data_basis"]["state"], "missing")
        self.assertFalse(empty["data_basis"]["gaps_known"])
        self.assertIn("mangler data", empty["data_basis"]["text"])
        paused = weekly_health([], events=[], restarts=[], controller={}, now=NOW,
                               history={"available": False, "state": "retrying", "next_retry_at": NOW + 60, "error": "disk"})
        self.assertEqual(paused["data_basis"]["state"], "paused")
        self.assertEqual(paused["data_basis"]["text"], "Historikken optager ikke – tallene mangler data")
        self.assertEqual(paused["data_basis"]["error"], "disk")
        unreadable = weekly_health([], events=[], restarts=[], controller={}, now=NOW,
                                   history={"available": True, "read_error": "locked"})
        self.assertEqual(unreadable["data_basis"]["state"], "unreadable")
        rows = [{"ts": NOW - 60 * i, **sample()} for i in range(10)]
        ok = weekly_health(rows, events=[], restarts=[], controller={}, now=NOW, history={"available": True})
        self.assertEqual(ok["data_basis"], {**ok["data_basis"], "state": "ok", "ok": True, "gaps_known": True, "text": ""})
        # Without a status (older caller) the rows alone decide.
        self.assertEqual(weekly_health(rows, events=[], restarts=[], controller={}, now=NOW)["data_basis"]["state"], "ok")

    def test_history_and_weekly_endpoints_report_the_recording(self):
        with tempfile.TemporaryDirectory() as tmp, ServerFixture(tmp) as fixture:
            owner, _csrf = fixture.server.auth.session("owner")
            history = fixture.server.history
            history.record = lambda *args, **kwargs: None  # no background sample races the paused state
            time.sleep(0.2)
            history.available, history.retry_at = False, time.time() + 600
            status, body = fixture.get("/history.json?range=1h", owner)
            self.assertEqual(status, 200)
            self.assertEqual(body["recording"]["state"], "retrying")
            status, body = fixture.get("/api/health/weekly", owner)
            self.assertEqual(status, 200, body)
            self.assertEqual(body["data_basis"]["state"], "paused")


def health(snapshot):
    rooms = snapshot.get("smart_ha_rooms", {})
    snapshot = {"smart_inputs_online": True, **snapshot}
    snapshot.setdefault("smart_room_quality", quality_rooms(rooms, 0.0, online=snapshot["smart_inputs_online"])[1])
    return ControllerObservability._ha_rooms_health(snapshot)


def fresh(**values):
    return {"measurement_age_version": 1, **{k: v for k, v in values.items()},
            **{f"{k}_age_s": 30 for k in values}}


class MissingSensorTests(unittest.TestCase):
    def runtime(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        return ControllerRuntime(gateway_state={}, hardware=HardwareAdapter(), state_path=Path(temp.name) / "c.json")

    def send(self, runtime, rooms):
        runtime.room_inputs({"source": "home_assistant", "valid_for_s": 180, "rooms": rooms})
        runtime.smart_inputs_received_monotonic = time.monotonic()
        return runtime.snapshot()

    def test_missing_sensors_is_validated_and_capped(self):
        kinds = ControllerRuntime._missing_sensor_kinds
        self.assertEqual(kinds(["co2", "pm2_5", "co2", "bogus", 7, None, "x" * 100, "temperature"]),
                         ["temperature", "co2", "pm25"])
        self.assertEqual(kinds("co2"), [])
        self.assertEqual(kinds({"co2": True}), [])
        self.assertEqual(kinds(None), [])
        # Only the first MAX_MISSING_SENSORS items are read.
        self.assertEqual(kinds(["bogus"] * ControllerRuntime.MAX_MISSING_SENSORS + ["co2"]), [])

    def test_a_missing_co2_turns_the_combined_status_from_fresh_to_mixed(self):
        runtime = self.runtime()
        snapshot = self.send(runtime, {"Stue": {**fresh(temperature=21.0), "missing_sensors": ["co2"]}})
        self.assertEqual(snapshot["smart_missing_sensors"], {"Stue": ["co2"]})
        item = ControllerObservability(runtime, path=Path(tempfile.gettempdir()) / "unused")._ha_rooms_health(snapshot)
        self.assertEqual(item["state"], "mixed")
        self.assertIs(item["fresh"], False)
        self.assertEqual((item["fresh_count"], item["missing_count"]), (1, 1))
        # The field never reaches the room values the control reads.
        self.assertNotIn("missing_sensors", runtime.smart_rooms["Stue"])

    def test_a_kind_with_a_value_is_not_missing_and_older_integrations_mean_none(self):
        runtime = self.runtime()
        snapshot = self.send(runtime, {"Stue": {**fresh(temperature=21.0, co2=700), "missing_sensors": ["co2"]}})
        self.assertEqual(snapshot["smart_missing_sensors"], {})
        snapshot = self.send(runtime, {"Stue": fresh(temperature=21.0), "Bad": {**fresh(humidity=60), "missing_sensors": "co2"}})
        item = health({k: snapshot[k] for k in ("smart_ha_rooms", "smart_missing_sensors", "smart_missing_rooms")})
        self.assertEqual((item["state"], item["missing_count"]), ("fresh", 0))

    def test_a_set_up_room_left_out_by_ha_counts_as_missing(self):
        runtime = self.runtime()
        runtime.config.configure({"room_setup": [
            {"name": "Stue", "type": "normal", "sensors": {"temperature": "sensor.stue_t"}},
            {"name": "Kontor", "type": "normal", "sensors": {"temperature": "sensor.k_t", "co2": "sensor.k_co2"}},
            {"name": "Loft", "type": "normal", "enabled": False, "sensors": {"co2": "sensor.loft"}},
            {"name": "Garage", "type": "excluded", "sensors": {"temperature": "sensor.g"}},
            {"name": "Gang", "type": "normal"},  # no sensor selected: neutral
        ]})
        snapshot = self.send(runtime, {"Stue": fresh(temperature=21.0)})
        self.assertEqual(snapshot["smart_missing_sensors"], {"Kontor": ["temperature", "co2"]})
        self.assertEqual(snapshot["smart_missing_rooms"], ["Kontor"])
        item = health({k: snapshot[k] for k in ("smart_ha_rooms", "smart_missing_sensors", "smart_missing_rooms")})
        self.assertEqual((item["state"], item["missing_count"], item["missing_rooms_count"]), ("mixed", 2, 1))
        self.assertEqual(item["rooms"], 2)

    def test_only_missing_reads_missing_and_offline_does_not_count(self):
        item = health({"smart_ha_rooms": {}, "smart_missing_sensors": {"Kontor": ["co2"]}, "smart_missing_rooms": ["Kontor"]})
        self.assertEqual((item["state"], item["fresh"], item["missing_count"]), ("missing", False, 1))
        item = health({"smart_ha_rooms": {"Stue": fresh(temperature=21.0)}, "smart_inputs_online": False,
                       "smart_missing_sensors": {"Kontor": ["co2"]}})
        self.assertEqual((item["state"], item["missing_count"]), ("offline", 0))
        # A room switched off in what HA sent never counts.
        item = health({"smart_ha_rooms": {"Stue": {**fresh(temperature=21.0), "enabled": False}},
                       "smart_missing_sensors": {"Stue": ["co2"]}})
        self.assertEqual(item["missing_count"], 0)
        # Stale and missing together are mixed.
        old = {"measurement_age_version": 1, "co2": 900, "co2_age_s": 5 * 3600}
        item = health({"smart_ha_rooms": {"Stue": old}, "smart_missing_sensors": {"Stue": ["temperature"]}})
        self.assertEqual(item["state"], "mixed")

    def test_the_weekly_report_repeats_the_missing_count_but_not_as_too_old(self):
        ha_rooms = {"state": "missing", "fresh": False, "connection_online": True, "fresh_count": 0,
                    "stale_count": 0, "unknown_age_count": 0, "missing_count": 1, "missing_rooms_count": 1}
        report = weekly_health([], events=[], restarts=[], now=NOW,
                               controller={"data_health": {"sensors": {"ha_rooms": ha_rooms}}})
        self.assertEqual(report["stale"]["ha_rooms"]["missing_count"], 1)
        self.assertEqual(report["stale"]["now"], [])


class RecentListTests(unittest.TestCase):
    def test_the_newest_ten_command_errors_and_restarts_newest_first(self):
        # The event log hands events newest first; restarts come oldest first.
        events = [{"kind": "alarm_raised", "code": "controller_command_failed", "time": NOW - 60 * i, "text": f"e{i}"}
                  for i in range(15)]
        restarts = [{"at": NOW - 3600 * (15 - i), "reason": f"r{i}"} for i in range(15)]
        report = weekly_health([], events=events, restarts=restarts, controller={}, now=NOW)
        self.assertEqual(report["command_errors"]["alarms"], 15)
        self.assertEqual([e["text"] for e in report["command_errors"]["recent"]], [f"e{i}" for i in range(10)])
        self.assertEqual(report["watchdog_restarts"]["count"], 15)
        self.assertEqual([r["reason"] for r in report["watchdog_restarts"]["recent"]], [f"r{i}" for i in range(14, 4, -1)])


def local(year, month, day, hour, minute):
    return time.mktime((year, month, day, hour, minute, 0, 0, 0, -1))


class MidnightGapTests(unittest.TestCase):
    def test_a_gap_over_midnight_is_split_over_the_days(self):
        rows = [{"ts": local(2033, 5, 17, 23, 49), **sample()}, {"ts": local(2033, 5, 17, 23, 50), **sample()},
                {"ts": local(2033, 5, 18, 0, 20), **sample()}]
        report = weekly_health(rows, events=[], restarts=[], controller={}, now=local(2033, 5, 18, 0, 21))
        days = {day["date"]: day["gap_minutes"] for day in report["per_day"]}
        self.assertEqual(days, {"2033-05-17": 10, "2033-05-18": 20})
        self.assertEqual(report["total"]["gap_minutes"], 30)

    def test_an_open_gap_over_midnight_is_split_too(self):
        rows = [{"ts": local(2033, 5, 17, 23, 49), **sample()}, {"ts": local(2033, 5, 17, 23, 50), **sample()}]
        report = weekly_health(rows, events=[], restarts=[], controller={}, now=local(2033, 5, 18, 0, 20))
        days = {day["date"]: day["gap_minutes"] for day in report["per_day"]}
        self.assertEqual(days, {"2033-05-17": 10, "2033-05-18": 20})
        self.assertEqual((report["total"]["gap_minutes"], report["open_gap_minutes"]), (30, 30))


class ContextFilterTests(unittest.TestCase):
    def rows(self):
        rows = []
        for i in range(60):  # 30 minutes normal (error +1 K), then 30 minutes frost (error -2 K)
            frost = i >= 30
            rows.append({"ts": NOW - 3600 + i * 60, "heating_coil_after_temperature": 20.0 if frost else 23.0,
                         "afterheat_target": 22.0, "afterheat_active": 1, "afterheat_context": 1.0 if frost else 0.0,
                         "afterheat_level": 3 + (i % 2 if frost else 0), "fan_supply_percent": 40 + (i == 10)})
        rows.append({"ts": NOW - 1200, **sample(), "afterheat_context": None})  # unknown, after a 20-minute gap
        return rows

    def test_precision_is_filtered_by_mode_but_gaps_and_alarms_are_not(self):
        events = [{"kind": "alarm_raised", "code": "controller_command_failed", "time": NOW - 60, "text": "x"}]
        all_modes = weekly_health(self.rows(), events=events, restarts=[], controller={}, now=NOW)
        normal = weekly_health(self.rows(), events=events, restarts=[], controller={}, now=NOW, context="0")
        frost = weekly_health(self.rows(), events=events, restarts=[], controller={}, now=NOW, context="1")
        unknown = weekly_health(self.rows(), events=events, restarts=[], controller={}, now=NOW, context="unknown")
        self.assertEqual(all_modes["context"], "all")
        self.assertEqual((normal["context"], normal["total"]["context_minutes"]), ("0", 30))
        self.assertEqual((normal["total"]["air_min"], normal["total"]["air_max"], normal["total"]["max_under"]), (23.0, 23.0, 0.0))
        self.assertEqual(normal["total"]["in_band_percent"], 0.0)
        self.assertEqual(normal["total"]["level_changes"], 0)
        self.assertEqual((frost["total"]["air_max"], frost["total"]["max_under"], frost["total"]["level_changes"]), (20.0, 2.0, 29))
        self.assertEqual(unknown["total"]["supply_minutes"], 1)
        self.assertEqual(all_modes["total"]["supply_minutes"], 61)
        for report in (normal, frost, unknown):
            self.assertEqual(report["total"]["samples"], 61)
            self.assertEqual(report["total"]["gap_minutes"], all_modes["total"]["gap_minutes"])
            self.assertEqual(report["total"]["fan_changes"], 2)
            self.assertEqual(report["command_errors"]["alarms"], 1)
        # An unknown filter value falls back to all modes.
        self.assertEqual(weekly_health(self.rows(), events=[], restarts=[], controller={}, now=NOW, context="bogus")["context"], "all")

    def test_the_endpoint_takes_the_mode_and_refuses_a_bad_one(self):
        with tempfile.TemporaryDirectory() as tmp, ServerFixture(tmp) as fixture:
            owner, _csrf = fixture.server.auth.session("owner")
            status, body = fixture.get("/api/health/weekly?context=1", owner)
            self.assertEqual((status, body["context"]), (200, "1"))
            self.assertEqual(fixture.get("/api/health/weekly?context=9", owner)[0], 400)


if __name__ == "__main__":
    unittest.main()
