"""Fixes after the 1.13.0-beta.1 review: watchdog exit, restart loop, stale alarms, retries and replay."""
import json
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "gateway"))

import control_replay
from controller_core import (FROST_LOCK_RETRY_MAX_SECONDS, RETRY_MAX_SECONDS, ControllerEngine,
                             ControllerState, HardwareAdapter)
from controller_runtime import ControllerRuntime
from diagnostics import Diagnostics, record_watchdog_restart
from master_arbitration import MasterArbitrator

try:
    import dantherm_gateway
except ImportError:  # serial/yaml/paho missing in a bare test environment
    dantherm_gateway = None


class ReviewTestCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def engine(self, **config):
        state = ControllerState(self.root / "controller.json")
        if config:
            state.configure(config)
        return ControllerEngine(state, HardwareAdapter())


class ExitPathTests(ReviewTestCase):
    def test_native_setpoint_falls_back_when_the_lock_is_held(self):
        engine = self.engine(afterheat_coil="water", afterheat_setpoint=21)
        engine.last_afterheat = {"command": 23.0, "native": 23}
        self.assertEqual(engine.afterheat_native_setpoint(lock_timeout=0.1), 23)
        held, release = threading.Event(), threading.Event()

        def stall():
            with engine.lock:
                held.set()
                release.wait(5)

        thread = threading.Thread(target=stall, daemon=True)
        thread.start()
        held.wait(2)
        try:
            started = time.monotonic()
            self.assertEqual(engine.afterheat_native_setpoint(lock_timeout=0.2), 21)
            self.assertLess(time.monotonic() - started, 2.0)
            # Frost latched: the configured setpoint is raised to the floor.
            engine.config.data.update({"frost_lock_latched": True, "frost_lock_enabled": True})
            self.assertEqual(engine.afterheat_native_setpoint(lock_timeout=0.1), 21)
            engine.config.data["afterheat_setpoint"] = 12
            self.assertEqual(engine.afterheat_native_setpoint(lock_timeout=0.1), 18)
            # Not under Pi control: nothing to restore.
            engine.last_afterheat = {"command": None, "native": 20}
            self.assertIsNone(engine.afterheat_native_setpoint(lock_timeout=0.1))
        finally:
            release.set()
            thread.join(2)

    @unittest.skipIf(dantherm_gateway is None, "gateway dependencies not installed")
    def test_watchdog_exit_arms_a_hard_exit_and_records_the_restart(self):
        gateway = dantherm_gateway.Gateway.__new__(dantherm_gateway.Gateway)
        gateway.controller = types.SimpleNamespace(config=types.SimpleNamespace(path=self.root / "controller.json"))
        timers = []

        class FakeTimer:
            def __init__(self, seconds, fn, args):
                self.seconds, self.fn, self.args, self.daemon, self.started = seconds, fn, args, False, False
                timers.append(self)

            def start(self):
                self.started = True

        with patch.object(dantherm_gateway.threading, "Timer", FakeTimer):
            error = gateway.watchdog_exit("Controller stalled")
            gateway.watchdog_exit("again")  # one timer only
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual(len(timers), 1)
        timer = timers[0]
        self.assertTrue(timer.started and timer.daemon)
        self.assertEqual(timer.seconds, dantherm_gateway.HARD_EXIT_SECONDS)
        self.assertIs(timer.fn, dantherm_gateway.os._exit)
        self.assertEqual(timer.args, (1,))
        saved = json.loads((self.root / "watchdog-restarts.json").read_text())
        self.assertEqual([e["reason"] for e in saved["restarts"]], ["Controller stalled", "again"])

    @unittest.skipIf(dantherm_gateway is None, "gateway dependencies not installed")
    def test_restore_on_exit_does_not_wait_for_a_held_engine_lock(self):
        engine = self.engine(afterheat_setpoint=22)
        engine.last_afterheat = {"command": 25.0, "native": 25}
        engine.last_applied["afterheat_setpoint"] = 25
        gateway = dantherm_gateway.Gateway.__new__(dantherm_gateway.Gateway)
        written = []
        gateway.write_afterheat_setpoint = lambda ser, value: written.append(value)
        gateway.controller = types.SimpleNamespace(engine=engine, master=types.SimpleNamespace(writes_allowed=lambda: True))
        held, release = threading.Event(), threading.Event()

        def stall():
            with engine.lock:
                held.set()
                release.wait(10)

        thread = threading.Thread(target=stall, daemon=True)
        thread.start()
        held.wait(2)
        try:
            started = time.monotonic()
            gateway.restore_afterheat_on_exit(None)
            self.assertLess(time.monotonic() - started, dantherm_gateway.EXIT_LOCK_TIMEOUT_SECONDS + 2)
        finally:
            release.set()
            thread.join(2)
        self.assertEqual(written, [22])

    def test_start_takes_a_monotonic_timestamp(self):
        runtime = ControllerRuntime(gateway_state={}, hardware=HardwareAdapter(),
                                    state_path=self.root / "controller.json", tick_seconds=60)
        self.assertIsNone(runtime.started_monotonic)
        before = time.monotonic()
        runtime.start()
        try:
            self.assertGreaterEqual(runtime.started_monotonic, before)
        finally:
            runtime.stop()

    @unittest.skipIf(dantherm_gateway is None, "gateway dependencies not installed")
    def test_watchdog_uses_monotonic_time_and_a_long_first_cycle_grace(self):
        source = (ROOT / "gateway" / "dantherm_gateway.py").read_text(encoding="utf-8")
        self.assertNotIn("time.time() - self.controller.engine.started_at", source)
        self.assertEqual(dantherm_gateway.CONTROLLER_FIRST_CYCLE_GRACE_SECONDS, 300.0)


class DiagnosticsTests(ReviewTestCase):
    @staticmethod
    def feed(outdoor=None, co2=None, outdoor_fresh=None, co2_fresh=None, **extra):
        def item(value, fresh):
            return {"available": value is not None, "fresh": bool(value is not None) if fresh is None else fresh,
                    "value": value}
        return {"data_health": {"sensors": {"outdoor": item(outdoor, outdoor_fresh), "co2": item(co2, co2_fresh)}},
                **extra}

    @staticmethod
    def codes(result):
        return [alarm["code"] for alarm in result["diagnostics_alarms"]]

    def test_a_sensor_never_seen_is_not_stale(self):
        diag = Diagnostics()
        for minute in range(0, 20):
            result = diag.update(self.feed(outdoor=5.0), 1000.0 + minute * 60)
        self.assertNotIn("local_sensor_stale", self.codes(result))

    def test_a_seen_sensor_that_goes_stale_alarms(self):
        diag = Diagnostics()
        diag.update(self.feed(outdoor=5.0, co2=600), 1000.0)
        for minute in range(1, 5):
            result = diag.update(self.feed(outdoor=5.0, co2=None), 1000.0 + minute * 60)
        self.assertIn("local_sensor_stale", self.codes(result))
        result = diag.update(self.feed(outdoor=5.0, co2=610), 1400.0)
        self.assertNotIn("local_sensor_stale", self.codes(result))

    def test_three_watchdog_restarts_in_30_minutes_is_critical(self):
        path = self.root / "diagnostics.json"
        restarts = self.root / "watchdog-restarts.json"
        now = 1_000_000.0
        record_watchdog_restart(restarts, "a", now - 3000)  # outside the window
        record_watchdog_restart(restarts, "b", now - 1000)
        record_watchdog_restart(restarts, "c", now - 500)
        diag = Diagnostics(path)
        result = diag.update({}, now)
        self.assertEqual(result["watchdog_restarts_30min"], 2)
        self.assertNotIn("watchdog_restarts", self.codes(result))
        record_watchdog_restart(restarts, "d", now - 10)
        result = diag.update({}, now + 1)
        self.assertIn("watchdog_restarts", self.codes(result))
        self.assertEqual(result["diagnostics_status"], "critical")
        # The window moves on: the alarm clears by itself.
        result = diag.update({}, now + 1000)
        self.assertNotIn("watchdog_restarts", self.codes(result))

    def test_failed_command_alarm_only_while_the_pi_may_write(self):
        diag = Diagnostics()
        pending = {"pending_commands": {"fan": {"error": "x"}}}
        diag.update({**pending, "hardware_writes_allowed": False}, 1000.0)
        result = diag.update({**pending, "hardware_writes_allowed": False}, 1100.0)
        self.assertNotIn("controller_command_failed", self.codes(result))
        diag.update({**pending, "hardware_writes_allowed": True}, 1200.0)
        result = diag.update({**pending, "hardware_writes_allowed": True}, 1300.0)
        self.assertIn("controller_command_failed", self.codes(result))


class MasterReleaseTests(ReviewTestCase):
    def test_failed_commands_are_cleared_when_the_pi_gives_up_master(self):
        runtime = ControllerRuntime(gateway_state={}, hardware=HardwareAdapter(),
                                    state_path=self.root / "controller.json")
        engine = runtime.engine
        engine.command_errors["fan"] = "fan: no ack"
        engine._failure_counts["fan"] = 4
        engine._failure_values["fan"] = (40, 40)
        engine._retry_at["fan"] = time.time() + 100
        engine.last_error = "fan: no ack"
        runtime._on_master_transition(MasterArbitrator.PI, MasterArbitrator.HCP4)
        self.assertEqual(engine.command_errors, {})
        self.assertEqual(engine._failure_counts, {})
        self.assertIsNone(engine.last_error)
        self.assertEqual(runtime.snapshot()["pending_commands"], {})


class RetryCapTests(ReviewTestCase):
    def test_frost_caps_an_already_scheduled_retry(self):
        engine = self.engine()
        calls = []
        now = time.time()
        engine._failure_values["afterheat_setpoint"] = 20
        engine._failure_counts["afterheat_setpoint"] = 6
        engine._retry_at["afterheat_setpoint"] = now + 290  # planned before the frost lock engaged
        engine._call("afterheat_setpoint", 20, calls.append, 20, keep_retrying=True)
        self.assertEqual(calls, [])
        self.assertLessEqual(engine._retry_at["afterheat_setpoint"] - now, FROST_LOCK_RETRY_MAX_SECONDS + 1)

    def test_a_retry_never_waits_beyond_the_maximum(self):
        engine = self.engine()
        now = time.time()
        engine._failure_values["fan"] = 1
        engine._retry_at["fan"] = now + 86400  # e.g. the clock was stepped back
        engine._call("fan", 1, lambda *_: None, 1)
        self.assertEqual(engine.last_applied.get("fan"), None)
        self.assertLessEqual(engine._retry_at["fan"] - now, RETRY_MAX_SECONDS + 1)


class ReplayTests(unittest.TestCase):
    def test_replay_yields_every_50_points(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = ControllerState(Path(tmp) / "controller.json").data
        samples = [{"ts": 1000.0 + i * 60, "co2": 600 + i, "humidity": 50} for i in range(120)]
        with patch.object(control_replay.time, "sleep") as sleep:
            result = control_replay.replay_local_auto(settings, samples, {"co2_setpoint": 900})
        self.assertEqual(len(result["points"]), 120)
        self.assertEqual(sleep.call_count, 2)
        sleep.assert_called_with(0)


if __name__ == "__main__":
    unittest.main()
