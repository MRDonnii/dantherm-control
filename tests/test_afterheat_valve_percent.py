"""Afterheat valve opening: HAC1 register 101 (0..10 = 0-10 V) is 10 % valve per level.

The history keeps the HAC1 level and the user's target in both control modes, so
the WebUI can draw the target ("Mål"), the setpoint sent to HAC1 and the opening.
"""
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "gateway"))

from controller_core import HardwareAdapter
from controller_runtime import ControllerRuntime
from dashboard_server import HISTORY_FIELDS, HistoryStore


class AfterheatValvePercentTests(unittest.TestCase):
    def make_runtime(self, gateway_state=None, **config):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        runtime = ControllerRuntime(
            gateway_state=gateway_state if gateway_state is not None else {},
            hardware=HardwareAdapter(),
            state_path=Path(temp.name) / "controller.json",
        )
        if config:
            runtime.config.configure(config)
        return runtime

    @staticmethod
    def level_state(level, age=1.0):
        return {"hac1_output_level": level, "hac1_output_level_monotonic": time.monotonic() - age}

    def test_fresh_level_gives_ten_percent_per_level(self):
        for level, percent in ((0, 0), (1, 10), (3, 30), (7, 70), (10, 100)):
            with self.subTest(level=level):
                snapshot = self.make_runtime(self.level_state(level)).snapshot()
                self.assertEqual(snapshot["actual_afterheat_level"], level)
                self.assertEqual(snapshot["actual_afterheat_valve_percent"], percent)

    def test_unknown_or_stale_level_has_no_percent(self):
        for state in ({}, self.level_state(4, age=31.0), {"hac1_output_level": 4},
                      self.level_state(None)):
            with self.subTest(state=state):
                snapshot = self.make_runtime(state).snapshot()
                self.assertIsNone(snapshot["actual_afterheat_level"])
                self.assertIsNone(snapshot["actual_afterheat_valve_percent"])

    def test_level_and_target_reach_the_history_state_in_both_modes(self):
        for mode in ("native", "pi"):
            with self.subTest(mode=mode):
                state = self.level_state(4)
                runtime = self.make_runtime(state, afterheat_setpoint=21.5, afterheat_control_mode=mode)
                runtime.refresh_measurements()
                runtime._publish_history_values(runtime.engine.resolve())
                self.assertEqual(state["afterheat_level"], 4)
                self.assertEqual(state["afterheat_target"], 21.5)
                # The decision keys always reach the history state (None while HAC1 steers itself).
                for key in ("afterheat_level_target", "afterheat_level_wanted"):
                    self.assertIn(key, state)
                if mode == "native":
                    self.assertIsNone(state["afterheat_level_target"])
                    self.assertIsNone(state["afterheat_level_wanted"])

    def test_history_store_records_level_target_and_setpoint(self):
        self.assertTrue({"afterheat_level", "afterheat_level_target", "afterheat_level_wanted",
                         "afterheat_target", "afterheat_setpoint"} <= set(HISTORY_FIELDS))
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = HistoryStore(Path(temp.name) / "history.sqlite3")
        store.record({"afterheat_level": 3, "afterheat_level_target": 4, "afterheat_level_wanted": 3.6,
                      "afterheat_target": 21.0, "afterheat_setpoint": 25}, now=time.time())
        rows = store.query("1h")
        self.assertTrue(rows)
        self.assertEqual(rows[-1]["afterheat_level"], 3.0)
        self.assertEqual(rows[-1]["afterheat_target"], 21.0)
        self.assertEqual(rows[-1]["afterheat_setpoint"], 25.0)
        self.assertEqual(rows[-1]["afterheat_level_target"], 4.0)
        self.assertAlmostEqual(rows[-1]["afterheat_level_wanted"], 3.6)

    def test_existing_history_gains_the_decision_column(self):
        import sqlite3
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "history.sqlite3"
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE samples (ts INTEGER PRIMARY KEY, "afterheat_level" REAL)')
        HistoryStore(path)
        with sqlite3.connect(path) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(samples)")}
        self.assertIn("afterheat_level_wanted", columns)


if __name__ == "__main__":
    unittest.main()
