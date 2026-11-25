"""1.13.0-beta.2: afterheat scale, afterheat replay, weekly health report, room recording retry."""
import copy
import json
import logging
import math
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "gateway"))
import control_history  # noqa: E402
import control_replay  # noqa: E402
from control_history import ControlHistory  # noqa: E402
from control_replay import learned_steps, replay_afterheat  # noqa: E402
from controller_core import ControllerError, HardwareAdapter  # noqa: E402
from dashboard_server import HistoryStore  # noqa: E402
from diagnostics import read_watchdog_restarts, record_watchdog_restart  # noqa: E402
from history_analysis import SCALE_FIELDS, afterheat_scale, before_coil, weekly_health  # noqa: E402

START = 1_000_020  # a whole minute


def scale_rows(lifts=(0.0, 2.0, 10.0), minutes=9, fan=36, before=18.0, **extra):
    """Each level held ``minutes`` minutes; the air settles at once (the analysis skips the first minutes anyway)."""
    rows, ts = [], START
    for level, lift in enumerate(lifts):
        for _ in range(minutes):
            rows.append({"ts": ts, "afterheat_level": level, "heating_coil_after_temperature": before + lift,
                         "afterheat_before_temperature": before, "fan_supply_percent": fan,
                         "return_temperature": 30.0 - level, "flow_temperature": 45.0, "afterheat_target": 22.0,
                         "afterheat_context": 0, **extra})
            ts += 60
    return rows


class AfterheatScaleTests(unittest.TestCase):
    def test_step_size_and_return_water_per_level_from_settled_minutes(self):
        result = afterheat_scale(scale_rows(), 24)
        self.assertEqual(result["before_source"], "measured")
        fan = result["fans"][0]
        self.assertEqual(fan["fan_percent"], 36)
        levels = fan["levels"]
        self.assertEqual(len(levels), 11)
        self.assertEqual([levels[i]["valve_percent"] for i in (0, 5, 10)], [0, 50, 100])
        # Nine minutes on a level, of which the last four count as settled (a step needs six minutes to arrive).
        self.assertEqual([levels[i]["minutes"] for i in range(3)], [4, 4, 4])
        self.assertEqual([levels[i]["lift"] for i in range(3)], [0.0, 2.0, 10.0])
        self.assertEqual([levels[i]["step_k"] for i in range(3)], [None, 2.0, 8.0])
        self.assertEqual([levels[i]["return_temperature"] for i in range(3)], [30.0, 29.0, 28.0])
        self.assertIsNone(levels[3]["air"])
        # The reference steps are at the pilot's 27 % fan and 20 K water lift (water 45, target 22).
        factor = 36 / 27 * 20 / 23
        self.assertAlmostEqual(result["reference_steps"][0], round(2.0 * factor, 2), places=2)
        self.assertAlmostEqual(result["reference_steps"][1], round(8.0 * factor, 2), places=2)
        self.assertIsNone(result["reference_steps"][2])

    def test_too_few_minutes_switched_off_or_summer_minutes_give_no_value(self):
        self.assertEqual(afterheat_scale(scale_rows(minutes=7), 24)["fans"][0]["levels"][1]["lift"], None)
        self.assertEqual(afterheat_scale(scale_rows(afterheat_context=4), 24)["fans"], [])
        self.assertEqual(afterheat_scale(scale_rows(outdoor_temp=18.0), 24)["fans"], [])

    def test_gap_and_fan_change_restart_the_settling(self):
        rows = scale_rows(minutes=12)
        for row in rows[8:]:
            row["ts"] += 600
        # Eight minutes before the gap (three settled) and four after it (not yet settled).
        self.assertEqual(afterheat_scale(rows, 24)["fans"][0]["levels"][0]["minutes"], 3)
        rows = scale_rows(minutes=12)
        rows[2]["fan_supply_percent"] = 50
        # The fan change restarts the count: nine minutes remain, four of them settled.
        self.assertEqual(afterheat_scale(rows, 24)["fans"][0]["levels"][0]["minutes"], 4)

    def test_before_coil_falls_back_to_the_estimate_from_t1_t3_t4(self):
        self.assertEqual(before_coil({"afterheat_before_temperature": 17.5}), (17.5, "measured"))
        value, source = before_coil({"outdoor_temp": 0.0, "extract_temp": 20.0, "exhaust_temp": 4.0})
        self.assertEqual(source, "estimated")
        self.assertAlmostEqual(value, 16.0)
        self.assertEqual(before_coil({"outdoor_temp": 0.0, "extract_temp": 20.0}), (None, None))
        self.assertEqual(before_coil({"outdoor_temp": 0.0, "extract_temp": 20.0, "exhaust_temp": 30.0}), (None, None))

    def test_history_rows_are_raw_bounded_and_hide_stale_samples(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = HistoryStore(Path(tmp) / "history.sqlite3")
            with store._connect() as db:
                db.executemany('INSERT INTO samples (ts, supply_temp, supply_temp_sample_fresh, afterheat_level) VALUES (?,?,?,?)',
                               [(1000, 20.0, 1, 1), (1060, 21.0, 0, 2), (1120, 22.0, 1, 3), (5000, 23.0, 1, 4)])
            rows = store.rows(200, ("supply_temp", "afterheat_level", "not_a_field"), markers=True, now=1150)
            self.assertEqual([row["ts"] for row in rows], [1000, 1060, 1120])
            self.assertEqual([row["supply_temp"] for row in rows], [20.0, None, 22.0])
            self.assertEqual(rows[1]["supply_temp_sample_fresh"], 0)
            self.assertNotIn("not_a_field", rows[0])

    def test_scale_endpoint_validates_the_period(self):
        with tempfile.TemporaryDirectory() as tmp, ServerFixture(tmp) as fixture:
            sid, _csrf = fixture.server.auth.session("owner")
            status, body = fixture.get("/api/afterheat/scale?hours=168", sid)
            self.assertEqual(status, 200)
            self.assertEqual(body["fans"], [])
            self.assertEqual(len(body["reference_steps"]), 10)
            self.assertEqual(fixture.get("/api/afterheat/scale?hours=5", sid)[0], 400)
            self.assertEqual(fixture.get("/api/afterheat/scale?hours=168")[0], 401)


def replay_rows(minutes=120, air=None, context=0, level=2, start=START):
    return [{"ts": start + i * 60,
             "heating_coil_after_temperature": air(i) if air else 21.6 + 1.2 * math.sin(i / 9),
             "afterheat_target": 22.0, "afterheat_level": level, "afterheat_active": 1,
             "fan_supply_percent": 36, "outdoor_temp": 8.0, "extract_temp": 21.0, "afterheat_context": context}
            for i in range(minutes)]


class AfterheatReplayTests(unittest.TestCase):
    settings = {"afterheat_pi_band": 0.5, "afterheat_pi_min_dwell_ticks": 3, "afterheat_control_mode": "pi"}

    def test_replay_is_deterministic_and_only_the_proposal_changes(self):
        settings = copy.deepcopy(self.settings)
        with patch("controller_core.ControllerEngine.apply", side_effect=AssertionError("hardware path")):
            same = replay_afterheat(settings, replay_rows(), {})
            wider = replay_afterheat(settings, replay_rows(), {"afterheat_pi_band": 1.5, "afterheat_pi_min_dwell_ticks": 6})
        self.assertEqual(settings, self.settings)
        self.assertEqual(same["scope"], "afterheat")
        self.assertEqual(same["summary"]["current"], same["summary"]["proposed"])
        self.assertEqual(same["summary"]["current"], wider["summary"]["current"])
        self.assertNotEqual(wider["summary"]["current"], wider["summary"]["proposed"])
        self.assertEqual(same["covered_minutes"], 119.0)
        self.assertEqual(len(same["points"]), 119)
        point = same["points"][0]
        self.assertEqual(set(point), {"ts", "target", "proposed_target", "actual_air", "actual_level",
                                      "current_air", "current_level", "proposed_air", "proposed_level"})
        self.assertEqual(same["summary"]["actual"]["level_changes"], 0)
        self.assertGreater(same["summary"]["current"]["level_changes"], 0)

    def test_target_offset_moves_the_proposed_target(self):
        result = replay_afterheat(self.settings, replay_rows(), {"afterheat_target_offset": 1.5})
        self.assertTrue(all(p["proposed_target"] == p["target"] + 1.5 for p in result["points"]))
        self.assertEqual(result["settings"]["proposed"]["target_offset"], 1.5)

    def test_invalid_settings_and_too_much_history_are_refused(self):
        for bad in ({"afterheat_setpoint": 30}, {"afterheat_pi_band": 5}, {"afterheat_pi_band": True},
                    {"afterheat_pi_min_dwell_ticks": 2.5}, {"afterheat_target_offset": float("nan")}, []):
            with self.assertRaises(ControllerError):
                replay_afterheat(self.settings, replay_rows(20), bad)
        with self.assertRaises(ControllerError):
            replay_afterheat(self.settings, replay_rows(800), {})

    def test_gaps_off_periods_and_short_stretches_are_left_out(self):
        rows = replay_rows(60) + replay_rows(60, start=START + 7200)
        self.assertEqual(replay_afterheat(self.settings, rows, {})["covered_minutes"], 118.0)
        off = replay_rows(30, context=4)
        self.assertEqual(replay_afterheat(self.settings, off, {})["points"], [])
        self.assertEqual(replay_afterheat(self.settings, replay_rows(5), {})["points"], [])

    def test_frost_lock_keeps_level_one_in_the_model(self):
        rows = replay_rows(90, air=lambda i: 26.0, context=1, level=1)
        result = replay_afterheat(self.settings, rows, {})
        self.assertEqual(result["frost_minutes"], 89)
        self.assertTrue(all(p["current_level"] >= 1 and p["proposed_level"] >= 1 for p in result["points"]))
        # History from before the context was recorded: frost from T1 near the lock limit.
        legacy = replay_rows(30, air=lambda i: 26.0, context=None, level=1)
        for row in legacy:
            row["outdoor_temp"] = 1.0
        self.assertEqual(replay_afterheat(self.settings, legacy, {})["frost_minutes"], 29)

    def test_step_model_prefers_learned_then_history_then_prior(self):
        result = replay_afterheat(self.settings, replay_rows(20), {}, [3.0] + [None] * 9, [None, 4.0] + [None] * 8)
        self.assertEqual(result["model"]["step_sources"][:3], ["history", "learned", "prior"])
        self.assertEqual(result["model"]["steps_k"][:3], [3.0, 4.0, 1.5])

    def test_learned_steps_reads_only_measured_steps(self):
        from afterheat_pilot import AfterheatPilot
        pilot = AfterheatPilot()
        self.assertEqual(learned_steps(pilot), [None] * 10)
        pilot.heat.learn(1, 6.0, 1.0, 100.0)
        steps = learned_steps(pilot)
        self.assertIsNone(steps[0])
        self.assertGreater(steps[1], 1.5)
        self.assertEqual(learned_steps(object()), [])

    def test_replay_does_not_write_to_the_live_pilot_log_but_others_still_do(self):
        from afterheat_pilot import LOG as PILOT_LOG
        records = []
        handler = logging.Handler()
        handler.emit = records.append
        PILOT_LOG.addHandler(handler)
        try:
            # This replay makes the pilot warn about frequent setpoint changes.
            replay_afterheat(self.settings, replay_rows(120, air=lambda i: 22 + 3 * math.sin(i)), {})
            replayed = len(records)
            thread = threading.Thread(target=lambda: PILOT_LOG.warning("live"))
            thread.start(); thread.join()
        finally:
            PILOT_LOG.removeHandler(handler)
        self.assertEqual(replayed, 0)
        self.assertEqual([r.getMessage() for r in records], ["live"])
        self.assertEqual(control_replay._QUIET_THREADS, set())

    def test_replay_endpoint_runs_afterheat_mode_without_changing_config(self):
        with tempfile.TemporaryDirectory() as tmp, ServerFixture(tmp) as fixture:
            sid, csrf = fixture.server.auth.session("owner")
            before = fixture.runtime.config.path.read_bytes()
            with patch.object(fixture.server.history, "rows", return_value=replay_rows(60)) as rows:
                status, body = fixture.post("/api/controller/replay", {"mode": "afterheat", "range": "6h",
                                            "settings": {"afterheat_pi_band": 1.0}}, sid, csrf)
            self.assertEqual(status, 200, body)
            self.assertEqual(body["scope"], "afterheat")
            self.assertEqual(rows.call_args_list[0].args[0], 21600)
            self.assertEqual(fixture.post("/api/controller/replay", {"mode": "afterheat", "range": "24h"}, sid, csrf)[0], 400)
            self.assertEqual(fixture.post("/api/controller/replay", {"mode": "afterheat", "range": "1h",
                                          "settings": {"afterheat_setpoint": 30}}, sid, csrf)[0], 400)
            self.assertTrue(control_replay.REPLAY_LOCK.acquire(timeout=2))
            try:
                self.assertEqual(fixture.post("/api/controller/replay", {"mode": "afterheat", "range": "1h"}, sid, csrf)[0], 429)
            finally:
                control_replay.REPLAY_LOCK.release()
            self.assertEqual(fixture.runtime.config.path.read_bytes(), before)


NOW = 2_000_000_000.0


def health_rows():
    rows = []
    for i in range(240):  # four hours
        ts = NOW - 4 * 3600 + i * 60
        rows.append({"ts": ts, "heating_coil_after_temperature": 22.0 + (1.0 if i % 2 else -1.0),
                     "afterheat_target": 22.0, "afterheat_active": 1, "afterheat_level": 1 + (i // 10) % 2,
                     "fan_supply_percent": 36, "afterheat_context": 0,
                     "supply_temp_sample_fresh": 0 if i < 5 else 1, "co2_sample_fresh": None})
    rows.append({"ts": NOW - 8 * 86400, "heating_coil_after_temperature": 40.0, "afterheat_target": 22.0,
                 "afterheat_active": 1})
    return rows


class WeeklyHealthTests(unittest.TestCase):
    def test_week_numbers(self):
        events = [{"kind": "alarm_raised", "code": "controller_command_failed", "time": NOW - 3600, "text": "kommando"},
                  {"kind": "alarm_raised", "code": "controller_command_failed", "time": NOW - 9 * 86400, "text": "gammel"},
                  {"kind": "alarm_cleared", "code": "controller_command_failed", "time": NOW - 3000, "text": "slut"},
                  {"kind": "alarm_raised", "code": "local_sensor_stale", "time": NOW - 100, "text": "stale"}]
        restarts = [{"at": NOW - 7200, "reason": "regulator stalled"}, {"at": NOW - 10 * 86400, "reason": "old"}]
        controller = {"pending_commands": {"fan": {"error": "x"}},
                      "data_health": {"sensors": {"co2": {"fresh": False}, "outdoor": {"fresh": True}}}}
        report = weekly_health(health_rows(), events=events, restarts=restarts, controller=controller, now=NOW)
        total = report["total"]
        self.assertEqual(total["samples"], 240)
        self.assertEqual(total["supply_minutes"], 240)
        self.assertEqual(total["air_min"], 21.0)
        self.assertEqual(total["air_max"], 23.0)
        self.assertEqual(total["error_sd"], 1.0)
        self.assertEqual(total["max_over"], 1.0)
        self.assertEqual(total["max_under"], 1.0)
        self.assertEqual(total["in_band_percent"], 0.0)
        self.assertEqual(total["level_changes"], 23)
        self.assertAlmostEqual(total["level_changes_per_hour"], round(23 / (239 / 60), 2))
        self.assertEqual(total["stale_minutes"]["supply_temp"], 5)
        self.assertEqual(total["stale_minutes"]["co2"], 0)
        self.assertEqual(sum(day["samples"] for day in report["per_day"]), 240)
        self.assertEqual(report["command_errors"]["alarms"], 1)
        self.assertEqual(report["command_errors"]["pending_now"], ["fan"])
        self.assertEqual(report["watchdog_restarts"]["count"], 1)
        self.assertEqual(report["watchdog_restarts"]["recent"][0]["reason"], "regulator stalled")
        self.assertEqual(report["stale"]["alarms"], 1)
        self.assertEqual(report["stale"]["now"], ["co2"])

    def test_gaps_are_counted_and_not_bridged(self):
        rows = health_rows()[:20]
        for row in rows[10:]:
            row["ts"] += 1800
        report = weekly_health(rows, events=[], restarts=[], controller={}, now=NOW)
        total = report["total"]
        # 31 min between samples plus the gap still open from the last sample to now.
        self.assertEqual(report["open_gap_minutes"], 191)
        self.assertEqual(total["gap_minutes"], 31 + 191)
        self.assertEqual(total["level_changes"], 0)
        self.assertIsNone(weekly_health([], events=[], restarts=[], controller={}, now=NOW)["total"]["error_sd"])

    def test_watchdog_restarts_are_kept_for_the_weekly_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "watchdog-restarts.json"
            record_watchdog_restart(path, "a", now=NOW - 6 * 86400)
            record_watchdog_restart(path, "b", now=NOW)
            self.assertEqual([e["reason"] for e in read_watchdog_restarts(path)], ["a", "b"])
            record_watchdog_restart(path, "c", now=NOW + 3 * 86400)
            self.assertEqual([e["reason"] for e in read_watchdog_restarts(path)], ["b", "c"])
            self.assertEqual(read_watchdog_restarts(Path(tmp) / "missing.json"), [])

    def test_endpoint_needs_diagnostics_permission(self):
        with tempfile.TemporaryDirectory() as tmp, ServerFixture(tmp) as fixture:
            record_watchdog_restart(fixture.runtime.config.path.parent / "watchdog-restarts.json", "test")
            owner, _csrf = fixture.server.auth.session("owner")
            status, body = fixture.get("/api/health/weekly", owner)
            self.assertEqual(status, 200, body)
            self.assertEqual(body["days"], 7)
            self.assertEqual(body["watchdog_restarts"]["count"], 1)
            reader, _csrf = fixture.server.auth.session("reader")
            self.assertEqual(fixture.get("/api/health/weekly", reader)[0], 403)


class RoomRecordingRetryTests(unittest.TestCase):
    def test_a_transient_failure_pauses_and_recording_resumes_with_backoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ControlHistory(Path(tmp) / "inputs.sqlite3")
            real_connect = sqlite3.connect
            calls = []

            def broken(*args, **kwargs):
                calls.append(1)
                raise sqlite3.OperationalError("disk I/O error")

            with self.assertLogs("control_history", level="DEBUG") as logs:
                with patch("control_history.sqlite3.connect", side_effect=broken):
                    store.record({"rooms": {}}, now=10_000)
                    self.assertFalse(store.available)
                    status = store.status()
                    self.assertEqual(status["state"], "retrying")
                    self.assertEqual(status["failures"], 1)
                    self.assertEqual(status["next_retry_at"], 10_060)
                    store.record({"rooms": {}}, now=10_030)  # before the retry: not even tried
                    self.assertEqual(len(calls), 1)
                    store.record({"rooms": {}}, now=10_060)  # retry fails: the pause doubles
                    self.assertEqual(store.status()["next_retry_at"], 10_180)
                    store.record({"rooms": {}}, now=10_180)
                    self.assertEqual(store.status()["next_retry_at"], 10_420)
                    self.assertEqual(len(calls), 3)
                with patch("control_history.sqlite3.connect", side_effect=real_connect):
                    store.record({"rooms": {"Bad": {}}}, now=10_420)
            warnings = [line for line in logs.output if line.startswith("WARNING")]
            self.assertEqual(len(warnings), 2, logs.output)  # paused once, resumed once
            self.assertIn("resumed", warnings[1])
            status = store.status()
            self.assertTrue(status["available"])
            self.assertEqual((status["state"], status["failures"], status["next_retry_at"]), ("recording", 0, None))
            self.assertEqual(status["recovered_at"], 10_420)
            with patch("control_history.time.time", return_value=10_500):
                self.assertEqual([row["ts"] for row in store.query(3600)], [10_420])

    def test_backoff_is_capped_and_a_clock_set_back_retries_at_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ControlHistory(Path(tmp) / "inputs.sqlite3")
            with patch("control_history.sqlite3.connect", side_effect=sqlite3.OperationalError("locked")):
                now = 0
                for _ in range(12):
                    store.record({}, now=now)
                    now = store.retry_at
                self.assertEqual(store.retry_at - store.failed_at, control_history.RETRY_MAX_SECONDS)
                self.assertTrue(store._retry_due(store.failed_at - 1000))

    def test_a_store_that_could_not_start_begins_recording_once_possible(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "state"
            blocker.write_text("not a directory")
            store = ControlHistory(blocker / "inputs.sqlite3")
            self.assertFalse(store.available)
            self.assertEqual(store.query(3600), [])
            blocker.unlink()
            store.record({"rooms": {}}, now=store.retry_at)
            self.assertTrue(store.available)
            self.assertTrue((blocker / "inputs.sqlite3").exists())


class ServerFixture:
    """A real controller dashboard on a free port with an owner and a plain user."""

    def __init__(self, tmp):
        self.root = Path(tmp)

    def __enter__(self):
        from controller_dashboard_server import ControllerDashboardHttpServer
        from controller_runtime import ControllerRuntime
        with patch.dict(os.environ, {"DANTHERM_WEBUI_AUTH_FILE": str(self.root / "auth.json")}):
            self.runtime = ControllerRuntime(gateway_state={}, hardware=HardwareAdapter(), state_path=self.root / "controller.json")
            self.server = ControllerDashboardHttpServer("127.0.0.1", 0, {}, "Test", None, history_path=self.root / "history.sqlite3",
                                                        controller_runtime=self.runtime)
        self.runtime.config.save()
        self.server.auth.save("owner", "long-test-password", True)
        self.server.auth.create_user("reader", "long-test-password", role="user")
        self.server.start()
        self.base = f"http://127.0.0.1:{self.server.server.server_address[1]}"
        return self

    def __exit__(self, *exc):
        self.server.stop()

    def _open(self, request):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            try:
                return error.code, json.load(error)
            except ValueError:
                return error.code, None

    def get(self, path, sid=None):
        headers = {"Cookie": "dantherm_session=" + sid} if sid else {}
        return self._open(urllib.request.Request(self.base + path, headers=headers))

    def post(self, path, body, sid, csrf):
        headers = {"Content-Type": "application/json", "Cookie": "dantherm_session=" + sid, "X-CSRF-Token": csrf}
        return self._open(urllib.request.Request(self.base + path, data=json.dumps(body).encode(), headers=headers))


if __name__ == "__main__":
    unittest.main()
