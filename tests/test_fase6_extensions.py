"""Phase 6 extensions: each a setting that is off by default.

Vacation lowers the afterheat target, free cooling holds the afterheat down,
the unit's own exchanger frost protection is kept in history, and the
dry-air limit lets go in steps.
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "gateway"))
from controller_core import ControllerEngine, ControllerState, HardwareAdapter  # noqa: E402
from dashboard_server import HISTORY_FIELDS, HistoryStore  # noqa: E402

T0 = 1_000_000.0


class Phase6Tests(unittest.TestCase):
    def make_engine(self, **config):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        state = ControllerState(Path(temp.name) / "controller.json")
        state.configure({"afterheat_enabled": True, "afterheat_setpoint": 22, **config})
        return ControllerEngine(state, HardwareAdapter())

    def test_new_settings_are_off_by_default(self):
        data = self.make_engine().config.data
        self.assertFalse(data["vacation_afterheat_enabled"])
        self.assertEqual(data["vacation_afterheat_drop_k"], 2.0)
        self.assertFalse(data["cooling_afterheat_hold_enabled"])
        self.assertFalse(data["dry_release_steps_enabled"])
        self.assertEqual(data["dry_release_step_ppm"], 200)

    # 1. Vacation lowers the afterheat target ---------------------------------
    def test_vacation_lowers_only_the_target(self):
        engine = self.make_engine(vacation_enabled=True)
        self.assertEqual(engine.resolve(T0)["afterheat_effective_setpoint"], 22)  # setting off
        engine.config.configure({"vacation_afterheat_enabled": True, "vacation_afterheat_drop_k": 3})
        result = engine.resolve(T0 + 5)
        self.assertEqual(result["afterheat_effective_setpoint"], 19)
        self.assertIn("ferie", result["afterheat_room_reason"])
        engine.config.configure({"vacation_enabled": False})
        self.assertEqual(engine.resolve(T0 + 10)["afterheat_effective_setpoint"], 22)

    # 2. Free cooling holds the afterheat down -------------------------------
    def free_cooling(self, engine):
        engine.update_measurements(room=26.0, outdoor=16.0)
        return engine.resolve(T0)

    def test_free_cooling_hold_sends_ten_degrees_when_on(self):
        cooling = {"cooling_enabled": True, "cooling_start_delay_seconds": 0}
        result = self.free_cooling(self.make_engine(**cooling))
        self.assertTrue(result["cooling_active"])
        self.assertEqual(result["afterheat_effective_setpoint"], 22)  # setting off: as before
        result = self.free_cooling(self.make_engine(**cooling, cooling_afterheat_hold_enabled=True))
        self.assertTrue(result["cooling_active"])
        self.assertEqual(result["afterheat_effective_setpoint"], 10)
        self.assertEqual(result["afterheat_room_reason"], "Frikøling: eftervarme holdt nede")

    # 3. The unit's own exchanger frost protection is recorded ---------------
    def test_unit_frost_protection_is_a_history_column(self):
        self.assertIn("unit_frost_protection", HISTORY_FIELDS)
        with tempfile.TemporaryDirectory() as temp:
            store = HistoryStore(Path(temp) / "h.sqlite3")
            store.record({"unit_frost_protection": True, "fan_supply_percent": 28}, now=T0)
            import sqlite3
            with sqlite3.connect(Path(temp) / "h.sqlite3") as db:
                row = db.execute('SELECT "unit_frost_protection", "fan_supply_percent" FROM samples').fetchone()
        self.assertEqual(row, (1.0, 28.0))

    # 4. Dry-air limit lets go in steps --------------------------------------
    def dry_levels(self, engine, co2_values):
        levels = []
        for i, co2 in enumerate(co2_values):
            engine.update_measurements(rh=15.0, co2=co2, outdoor=-2)
            engine.set_external({"extract_temp": 21.5, "indoor_rh_average": 25.0, "indoor_rh_lowest": 23.0})
            levels.append(engine.resolve(T0 + i * 5)["effective_level"])
        return levels

    def test_dry_release_steps_from_the_dry_level(self):
        dry = {"dry_protection_enabled": True, "dry_rh_limit": 30, "dry_max_level": 2, "co2_setpoint": 800}
        trace = [650] * 3 + [810] * 3 + [1010] * 3
        old = self.dry_levels(self.make_engine(**dry), trace)
        new = self.dry_levels(self.make_engine(**dry, dry_release_steps_enabled=True), trace)
        self.assertEqual(old[2], 2)
        self.assertEqual(new[2], 2)
        self.assertGreater(old[5], 3)  # before: straight to the full CO2 level
        self.assertEqual(new[5], 3)    # now: one step over the dry level
        self.assertLessEqual(new[8], 4)


if __name__ == "__main__":
    unittest.main()
