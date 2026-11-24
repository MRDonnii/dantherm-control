"""1.13.0-beta.7: regulation quality before and after a software update (read-only),
and the version timeline it is built on (gateway start + update backups)."""
import io
import json
import os
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "gateway"))
sys.path.insert(0, str(Path(__file__).parent))
import update_history  # noqa: E402
from history_analysis import (COMPARE_MIN_MINUTES, COMPARE_SETTLE_S, comparison_windows,  # noqa: E402
                              update_comparison)
from update_history import (UPDATE_BACKFILL_FILE, UPDATE_HISTORY_FILE, backfill, merged_updates,  # noqa: E402
                            record_start)

DAY = 86400
T = 1_800_000_000  # the update under test (a whole minute)


def minutes(start, end, *, error=0.0, context=0, level=lambda i: 3, fan=40, outdoor=2.0, active=1, target=22.0):
    """One sample a minute from ``start`` up to (not including) ``end``."""
    rows = []
    for i, ts in enumerate(range(int(start), int(end), 60)):
        err = error(i) if callable(error) else error
        rows.append({"ts": ts, "heating_coil_after_temperature": target + err, "afterheat_target": target,
                     "afterheat_active": active, "afterheat_level": level(i), "fan_supply_percent": fan,
                     "afterheat_context": context, "outdoor_temp": outdoor})
    return rows


def updates(*times):
    return [{"id": str(t), "version": f"1.{n}", "previous_version": f"1.{n - 1}" if n else None, "at": t}
            for n, t in enumerate(times)]


def compare(before_rows, after_rows, *, ups=None, index=1, window=DAY, now=T + 10 * DAY, **kwargs):
    ups = ups or updates(T - 10 * DAY, T)
    windows = comparison_windows(ups, index, window, now)
    return update_comparison(ups[index], windows, before_rows, after_rows, **kwargs)


def metric(result, key):
    return next(item for item in result["metrics"] if item["key"] == key)


class WindowTests(unittest.TestCase):
    def test_a_sample_at_the_version_boundary_is_not_counted_in_the_old_version(self):
        before = minutes(T - DAY, T, error=0.2) + minutes(T, T + 60, error=9.0)
        after = minutes(T + COMPARE_SETTLE_S, T + DAY, error=0.1)
        result = compare(before, after)
        self.assertEqual(result["before"]["samples"], DAY // 60)
        self.assertEqual(result["before"]["max_over"], 0.2)
        self.assertLessEqual(result["before"]["coverage_percent"], 100)

    def test_before_window_stops_at_the_previous_update_and_after_at_the_next(self):
        ups = updates(T - 5 * 3600, T, T + 2 * DAY)
        windows = comparison_windows(ups, 1, 3 * DAY, now=T + 10 * DAY)
        # Before: from the previous version's settled start, not 3 days back.
        self.assertEqual(windows["before"]["start"], T - 5 * 3600 + COMPARE_SETTLE_S)
        self.assertEqual(windows["before"]["end"], T)
        self.assertTrue(windows["before"]["cut_by_previous_update"])
        # After: from 30 min after the update up to the next update.
        self.assertEqual(windows["after"]["start"], T + COMPARE_SETTLE_S)
        self.assertEqual(windows["after"]["end"], T + 2 * DAY)
        self.assertTrue(windows["after"]["cut_by_next_update"])
        self.assertFalse(windows["after"]["ongoing"])

    def test_full_windows_and_an_after_window_that_is_still_running(self):
        ups = updates(T - 10 * DAY, T)
        windows = comparison_windows(ups, 1, DAY, now=T + 3600)
        self.assertEqual(windows["before"]["start"], T - DAY)
        self.assertFalse(windows["before"]["cut_by_previous_update"])
        self.assertEqual(windows["after"]["end"], T + 3600)
        self.assertTrue(windows["after"]["ongoing"])

    def test_previous_update_within_the_settle_time_leaves_an_empty_before_window(self):
        windows = comparison_windows(updates(T - 600, T), 1, DAY, now=T + 2 * DAY)
        self.assertEqual(windows["before"]["start"], windows["before"]["end"])

    def test_rows_of_another_version_and_the_settle_time_are_left_out(self):
        # The previous version (bad) before T - 12 h; the settle half hour after T is bad too.
        ups = updates(T - 12 * 3600, T)
        older = minutes(T - DAY, T - 12 * 3600, error=5.0)
        before = minutes(T - 12 * 3600, T, error=0.2)
        settle = minutes(T, T + COMPARE_SETTLE_S, error=-6.0)
        after = minutes(T + COMPARE_SETTLE_S, T + DAY, error=0.2)
        result = compare(older + before, settle + after, ups=ups)
        self.assertEqual(result["before"]["error_mean"], 0.2)
        self.assertEqual(result["before"]["max_over"], 0.2)
        # The previous version's settle half hour is outside the before window as well.
        self.assertEqual(result["before"]["samples"], 12 * 60 - COMPARE_SETTLE_S // 60)
        self.assertEqual(result["after"]["error_mean"], 0.2)
        self.assertEqual(result["after"]["max_under"], 0.0)
        self.assertEqual(result["after"]["samples"], (DAY - COMPARE_SETTLE_S) // 60)


class MetricTests(unittest.TestCase):
    def test_mode_filter_only_counts_the_chosen_operating_mode(self):
        rows_b = minutes(T - DAY, T - 12 * 3600, context=1, error=0.1) + minutes(T - 12 * 3600, T, context=2, error=3.0)
        rows_a = minutes(T + COMPARE_SETTLE_S, T + DAY, context=1, error=0.1)
        result = compare(rows_b, rows_a, context="1")
        self.assertEqual(result["context"], "1")
        self.assertEqual(result["before"]["context_minutes"], 12 * 60)
        self.assertEqual(result["before"]["samples"], DAY // 60)
        self.assertEqual(result["before"]["error_mean"], 0.1)
        self.assertEqual(metric(result, "error_mean")["verdict"], "unchanged")
        everything = compare(rows_b, rows_a)
        self.assertGreater(everything["before"]["error_mean"], 1)
        self.assertEqual(metric(everything, "error_mean")["verdict"], "better")

    def test_too_little_data_gives_no_verdict(self):
        short = minutes(T - (COMPARE_MIN_MINUTES - 10) * 60, T, error=2.0)
        result = compare(short, minutes(T + COMPARE_SETTLE_S, T + DAY, error=0.0))
        self.assertFalse(result["enough"])
        self.assertFalse(result["before"]["enough"])
        self.assertTrue(result["after"]["enough"])
        self.assertTrue(result["summary"].startswith("For lidt data før opdateringen"))
        verdicts = {item["verdict"] for item in result["metrics"]}
        self.assertNotIn("better", verdicts)
        self.assertNotIn("worse", verdicts)
        self.assertEqual(metric(result, "error_sd")["verdict_text"], "For lidt data")
        # The coverage line still tells what there was.
        self.assertEqual(result["before"]["supply_minutes"], COMPARE_MIN_MINUTES - 10)
        self.assertGreater(result["before"]["gap_minutes"], 0)

    def test_minutes_with_the_afterheat_off_do_not_count_as_measured(self):
        rows = minutes(T - DAY, T, active=0)
        result = compare(rows, minutes(T + COMPARE_SETTLE_S, T + DAY))
        self.assertEqual(result["before"]["context_minutes"], DAY // 60)
        self.assertEqual(result["before"]["supply_minutes"], 0)
        self.assertFalse(result["enough"])

    def test_small_differences_are_unchanged_and_large_ones_better_or_worse(self):
        wobble = lambda size: (lambda i: size if i % 2 else -size)  # noqa: E731
        before = minutes(T - DAY, T, error=wobble(0.5))
        self.assertEqual(metric(compare(before, minutes(T + COMPARE_SETTLE_S, T + DAY, error=wobble(0.55))),
                                "error_sd")["verdict"], "unchanged")
        better = compare(before, minutes(T + COMPARE_SETTLE_S, T + DAY, error=wobble(0.2)))
        self.assertEqual(metric(better, "error_sd")["verdict"], "better")
        self.assertEqual(metric(better, "error_sd")["verdict_text"], "Bedre")
        self.assertEqual(metric(better, "error_sd")["delta"], -0.3)
        self.assertEqual(metric(better, "max_over")["verdict"], "better")
        worse = compare(before, minutes(T + COMPARE_SETTLE_S, T + DAY, error=wobble(1.5)))
        self.assertEqual(metric(worse, "error_sd")["verdict_text"], "Dårligere")
        self.assertEqual(metric(worse, "in_band_percent")["verdict"], "worse")
        self.assertEqual(worse["summary"].split(" · ")[1], f"{worse['counts']['worse']} dårligere")

    def test_mean_error_is_judged_by_its_distance_from_zero(self):
        result = compare(minutes(T - DAY, T, error=0.6), minutes(T + COMPARE_SETTLE_S, T + DAY, error=-0.3))
        item = metric(result, "error_mean")
        self.assertEqual((item["before"], item["after"], item["delta"]), (0.6, -0.3, -0.9))
        self.assertEqual(item["verdict"], "better")

    def test_level_changes_use_a_relative_threshold_too(self):
        every = lambda n: (lambda i: 3 + (i // n) % 2)  # noqa: E731 - a level change every n minutes
        before = minutes(T - DAY, T, level=every(6))  # 10 per hour
        similar = compare(before, minutes(T + COMPARE_SETTLE_S, T + DAY, level=every(7)))  # ~8.6 per hour
        self.assertEqual(metric(similar, "level_changes_per_hour")["verdict"], "unchanged")
        calmer = compare(before, minutes(T + COMPARE_SETTLE_S, T + DAY, level=every(20)))  # 3 per hour
        self.assertEqual(metric(calmer, "level_changes_per_hour")["verdict"], "better")
        # Fan changes are shown but never judged (they follow use, not regulation).
        self.assertFalse(metric(calmer, "fan_changes_per_hour")["judged"])
        self.assertEqual(metric(calmer, "fan_changes_per_hour")["verdict"], "none")

    def test_command_errors_and_restarts_are_counted_per_window(self):
        events = [{"kind": "alarm_raised", "code": "controller_command_failed", "time": T + 3600 * h} for h in (2, 3, 4)]
        events.append({"kind": "alarm_raised", "code": "controller_command_failed", "time": T + 600})  # settle time
        restarts = [{"at": T - 3600, "reason": "stalled"}]
        result = compare(minutes(T - DAY, T), minutes(T + COMPARE_SETTLE_S, T + DAY), events=events, restarts=restarts)
        self.assertEqual((result["before"]["command_errors"], result["after"]["command_errors"]), (0, 3))
        self.assertEqual(metric(result, "command_errors")["verdict"], "worse")
        self.assertEqual((result["before"]["watchdog_restarts"], result["after"]["watchdog_restarts"]), (1, 0))
        self.assertEqual(metric(result, "watchdog_restarts")["verdict"], "better")
        # Older than what the logs keep: unknown, not 0.
        unknown = compare(minutes(T - DAY, T), minutes(T + COMPARE_SETTLE_S, T + DAY),
                          events=events, events_known_since=T, restarts=restarts, restarts_known_since=T + DAY)
        self.assertIsNone(unknown["before"]["command_errors"])
        self.assertIsNone(unknown["after"]["watchdog_restarts"])
        self.assertEqual(metric(unknown, "watchdog_restarts")["verdict"], "none")

    def test_coverage_and_outdoor_conditions_per_side(self):
        before = minutes(T - DAY, T - 6 * 3600, outdoor=-2.0) + minutes(T - 3 * 3600, T, outdoor=4.0)
        result = compare(before, minutes(T + COMPARE_SETTLE_S, T + DAY, outdoor=8.0))
        side = result["before"]
        self.assertEqual(side["window_minutes"], DAY // 60)
        self.assertEqual(side["gap_minutes"], 3 * 60 + 1)
        self.assertEqual((side["outdoor_min"], side["outdoor_max"]), (-2.0, 4.0))
        self.assertEqual(result["after"]["outdoor_mean"], 8.0)
        self.assertLess(side["coverage_percent"], 90)
        self.assertIn("Vejr og brug", result["note"])


def make_backup(root, name, version, mtime, before=3):
    """An update backup whose archive holds the replaced build's VERSION."""
    folder = Path(root) / name
    folder.mkdir()
    with tarfile.open(folder / "app-code.tar.gz", "w:gz") as tar:
        for index in range(before):
            info = tarfile.TarInfo(f"./file{index}.py"); data = b"x = 1\n"; info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        data = f"{version}\n".encode()
        info = tarfile.TarInfo("./VERSION"); info.size = len(data); info.mtime = mtime
        tar.addfile(info, io.BytesIO(data))


class VersionRecordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.version = self.dir / "VERSION"
        self.history = self.dir / UPDATE_HISTORY_FILE

    def tearDown(self):
        self.tmp.cleanup()

    def install(self, version, at):
        self.version.write_text(version + "\n")
        os.utime(self.version, (at, at))

    def test_a_new_version_is_recorded_once_with_its_install_time(self):
        self.install("1.13.0-beta.6", T)
        self.assertEqual(record_start(self.history, self.version, now=T + 30), {"version": "1.13.0-beta.6", "at": T})
        # The same version again (a plain restart): nothing new.
        self.assertIsNone(record_start(self.history, self.version, now=T + 3600))
        self.install("1.13.0-beta.7", T + DAY)
        record_start(self.history, self.version, now=T + DAY + 20)
        saved = json.loads(self.history.read_text())["entries"]
        self.assertEqual([(e["version"], e["at"]) for e in saved], [("1.13.0-beta.6", T), ("1.13.0-beta.7", T + DAY)])

    def test_an_implausible_file_time_falls_back_to_now_and_the_list_is_capped(self):
        self.install("2.0.0", T + 10 * DAY)  # in the future
        self.assertEqual(record_start(self.history, self.version, now=T)["at"], T)
        for n in range(60):
            self.install(f"2.0.{n + 1}", T + (n + 1) * 60)
            record_start(self.history, self.version, now=T + (n + 1) * 60 + 5)
        saved = json.loads(self.history.read_text())["entries"]
        self.assertEqual(len(saved), update_history.MAX_ENTRIES)
        self.assertEqual(saved[-1]["version"], "2.0.60")

    def test_missing_or_garbled_version_records_nothing(self):
        self.assertIsNone(record_start(self.history, self.dir / "missing", now=T))
        self.version.write_text("<script>\n")
        self.assertIsNone(record_start(self.history, self.version, now=T))
        self.assertFalse(self.history.exists())

    def test_backups_give_each_replaced_version_and_its_install_time(self):
        backups = self.dir / "backups"; backups.mkdir()
        stamp = lambda ts: time.strftime("%Y%m%d-%H%M%S", time.localtime(ts))  # noqa: E731
        make_backup(backups, f"hch5-control-update-{stamp(T)}", "1.12.3", T - DAY)
        make_backup(backups, f"hch5-control-update-{stamp(T + 600)}", "1.13.0-beta.1", T)
        # A failed attempt (rolled back) holds the same install again.
        make_backup(backups, f"hch5-control-update-{stamp(T + 1200)}", "1.13.0-beta.1", T)
        make_backup(backups, f"hch5-control-update-{stamp(T - 90 * DAY)}", "0.9.0", T - 91 * DAY)  # too old
        (backups / "hch5-control-update-garbage").mkdir()
        out = self.dir / UPDATE_BACKFILL_FILE
        summary = backfill(backups, out, now=T + 3600)
        self.assertEqual(summary, {"read": 3, "left": 0, "entries": 2})
        saved = json.loads(out.read_text())
        self.assertEqual([(e["version"], e["at"]) for e in saved["entries"]], [("1.12.3", T - DAY), ("1.13.0-beta.1", T)])
        # Backups already read are not opened again.
        self.assertEqual(backfill(backups, out, now=T + 3600)["read"], 0)
        # The gateway's own record of the running version joins the timeline.
        self.install("1.13.0-beta.2", T + 600)
        record_start(self.history, self.version, now=T + 700)
        timeline = merged_updates(self.dir)
        self.assertEqual([(u["version"], u["previous_version"]) for u in timeline],
                         [("1.12.3", None), ("1.13.0-beta.1", "1.12.3"), ("1.13.0-beta.2", "1.13.0-beta.1")])
        self.assertEqual(timeline[1]["id"], str(T))
        self.assertEqual([u["source"] for u in timeline], ["backup", "backup", "start"])

    def test_the_backfill_reads_the_newest_backups_first_within_its_budget(self):
        backups = self.dir / "backups"; backups.mkdir()
        for n in range(3):
            make_backup(backups, f"hch5-control-update-{time.strftime('%Y%m%d-%H%M%S', time.localtime(T + n * 60))}",
                        f"1.{n}", T - 3600 + n * 60)
        out = self.dir / UPDATE_BACKFILL_FILE
        # Out of time at once: still the newest one, the rest at the next update.
        summary = backfill(backups, out, now=T + 3600, budget_s=-1)
        self.assertEqual((summary["read"], summary["left"]), (1, 2))
        self.assertEqual([e["version"] for e in json.loads(out.read_text())["entries"]], ["1.2"])
        self.assertEqual(backfill(backups, out, now=T + 3600)["entries"], 3)


class EndpointTests(unittest.TestCase):
    def test_permission_validation_and_answer(self):
        from test_beta2_features import ServerFixture
        with tempfile.TemporaryDirectory() as tmp, ServerFixture(tmp) as fixture:
            owner, _csrf = fixture.server.auth.session("owner")
            reader, _csrf = fixture.server.auth.session("reader")
            path = "/api/health/update-comparison"
            self.assertEqual(fixture.get(path, reader)[0], 403)
            self.assertEqual(fixture.get(path)[0], 401)
            # The gateway recorded its own version at start; with no earlier one there is nothing to compare.
            recorded = json.loads((Path(tmp) / UPDATE_HISTORY_FILE).read_text())["entries"]
            self.assertEqual(len(recorded), 1)
            status, body = fixture.get(path, owner)
            self.assertEqual(status, 200, body)
            self.assertEqual((body["updates"], body["comparison"]), ([], None))
            now = time.time()
            (Path(tmp) / UPDATE_BACKFILL_FILE).write_text(json.dumps({"entries": [
                {"version": "1.0.0", "at": now - 9 * DAY}, {"version": "1.1.0", "at": now - 2 * DAY}]}))
            status, body = fixture.get(path + "?window=3d&context=1", owner)
            self.assertEqual(status, 200, body)
            self.assertEqual([u["version"] for u in body["updates"]][-1], "1.1.0")
            newest = body["updates"][0]
            self.assertEqual(body["comparison"]["update"]["id"], newest["id"])
            self.assertEqual((body["comparison"]["window"], body["comparison"]["context"]), ("3d", "1"))
            self.assertFalse(body["comparison"]["enough"])
            older = body["updates"][-1]["id"]
            status, body = fixture.get(f"{path}?update={older}&window=7d", owner)
            self.assertEqual(status, 200, body)
            self.assertEqual(body["comparison"]["update"]["version"], "1.1.0")
            for bad in ("?window=2d", "?context=9", "?update=abc", "?update=123", f"?update={older}&window=1h"):
                self.assertEqual(fixture.get(path + bad, owner)[0], 400, bad)


if __name__ == "__main__":
    unittest.main()
