"""History is averaged per time bucket, with a min/max spread for T2AH."""
import importlib.util, tempfile, time, unittest
from pathlib import Path
ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location("dashboard_server", ROOT / "gateway/dashboard_server.py")
MODULE = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(MODULE)


def record(store, now, **values):
    store.last_sample = 0
    state = dict(values)
    if "heating_coil_after_temperature" in values:
        state["heating_coil_after_temperature_sample_monotonic"] = time.monotonic()
    store.record(state, now=now)


class HistoryBucketTests(unittest.TestCase):
    def test_long_ranges_average_buckets_and_keep_the_t2ah_spread(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MODULE.HistoryStore(Path(tmp) / "history.sqlite3", sample_seconds=10)
            start = time.time() - 6 * 3600
            for minute in range(6 * 60):
                swing = 20.0 if minute % 6 < 3 else 25.0
                record(store, start + minute * 60, outdoor_temp=-1.0, heating_coil_after_temperature=swing,
                       afterheat_active=minute % 2 == 0, afterheat_setpoint=21)
            rows = store.query("30d")
            self.assertLessEqual(len(rows), MODULE.HistoryStore.MAX_POINTS)
            full = [row for row in rows if row["heating_coil_after_temperature_min"] is not None]
            self.assertTrue(full)
            row = full[len(full) // 2]
            self.assertEqual(row["heating_coil_after_temperature_min"], 20.0)
            self.assertEqual(row["heating_coil_after_temperature_max"], 25.0)
            self.assertAlmostEqual(row["afterheat_active"], 0.5, places=1)
            self.assertEqual(row["afterheat_setpoint"], 21.0)
            self.assertEqual(row["outdoor_temp"], -1.0)

    def test_short_ranges_keep_every_sample(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MODULE.HistoryStore(Path(tmp) / "history.sqlite3", sample_seconds=60)
            start = time.time() - 1800
            for minute in range(30):
                record(store, start + minute * 60, outdoor_temp=float(minute))
            rows = store.query("1h")
            self.assertEqual([row["outdoor_temp"] for row in rows], [float(minute) for minute in range(30)])


if __name__ == "__main__":
    unittest.main()
