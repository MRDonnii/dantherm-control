"""Dry-air limit flapping (2026-11-19: 626 level changes in a day).

The unit's own RH read 13-17 % while the rooms read 38-43 %. The dry-air
limit used the unit RH and had no hysteresis or hold, so every CO2 wobble
around its setpoint flipped the level between 2 and 4 (sometimes every 5 s).
"""
import math
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "gateway"))
from advanced_control import absolute_humidity  # noqa: E402
from controller_core import ControllerEngine, ControllerError, ControllerState, HardwareAdapter  # noqa: E402
from controller_runtime import ControllerRuntime  # noqa: E402
from diagnostics import (  # noqa: E402
    LEVEL_FLAP_CLEAR, LEVEL_FLAP_LIMIT, LEVEL_FLAP_QUIET_SECONDS, LEVEL_FLAP_REARM_SECONDS, Diagnostics,
)

T0 = 1_000_000.0
TICK = 5


class DryAirFlappingTests(unittest.TestCase):
    def make_engine(self, **config):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        state = ControllerState(Path(temp.name) / "controller.json")
        state.configure({"dry_protection_enabled": True, "dry_rh_limit": 30, "dry_max_level": 2, **config})
        return ControllerEngine(state, HardwareAdapter())

    def run_hour(self, engine, rooms, co2_wobble=(780, 830), unit_rh=15.0, rooms_wobble=None):
        """One hour of 5 s ticks with CO2 wobbling around the 800 ppm setpoint."""
        levels, result = [], None
        for i in range(3600 // TICK):
            now = T0 + i * TICK
            engine.update_measurements(rh=unit_rh, co2=co2_wobble[i % 2], outdoor=-2)
            external = {"extract_temp": 21.5}
            room_rh = rooms_wobble[(i // 6) % 2] if rooms_wobble else rooms
            if room_rh is not None:
                external.update({"indoor_rh_average": room_rh, "indoor_rh_lowest": room_rh - 2})
            engine.set_external(external)
            result = engine.resolve(now)
            levels.append(result["effective_level"])
        changes = sum(1 for a, b in zip(levels, levels[1:]) if a != b)
        return changes, levels, result

    def test_unit_rh_15_rooms_40_no_longer_flaps(self):
        engine = self.make_engine()
        changes, levels, result = self.run_hour(engine, rooms=40.0)
        # The rooms are not dry: the limit stays off and the level follows CO2 calmly.
        self.assertLessEqual(changes, 2, levels[:40])
        self.assertFalse(result["dry_protection_active"])
        self.assertEqual(result["dry_protection_state"], "standby")
        self.assertEqual(result["dry_protection_indoor_rh"], 40.0)
        self.assertEqual(result["level_changes_last_hour"], changes)

    def test_stale_ha_never_falls_back_to_the_unit_rh(self):
        engine = self.make_engine()
        changes, levels, result = self.run_hour(engine, rooms=None)
        self.assertLessEqual(changes, 2, levels[:40])
        self.assertFalse(result["dry_protection_active"])
        self.assertEqual(result["dry_protection_state"], "sensor_missing")
        self.assertIsNone(result["dry_protection_indoor_rh"])

    def test_really_dry_rooms_with_co2_wobbling_at_the_setpoint_stay_released(self):
        # Rooms at 25 % really are dry; CO2 wobbles over and under its setpoint.
        engine = self.make_engine()
        changes, levels, result = self.run_hour(engine, rooms=25.0)
        # High CO2 ends the limit at once, and it only starts again at 700 ppm,
        # which this CO2 never reaches: no flapping, however long the hold.
        self.assertLessEqual(changes, 3, levels[:40])
        self.assertLess(result["level_changes_last_hour"], LEVEL_FLAP_CLEAR)
        self.assertFalse(result["dry_protection_active"])
        self.assertIn(result["dry_protection_state"], ("co2_high", "co2_wait"))

    def test_closed_loop_co2_cycles_calmly(self):
        """CO2 follows the level: the limit lets it rise, the release lets it fall.

        Each tick CO2 moves towards the level's equilibrium with the house's
        air-change time (30 min): about 950 ppm on level 2, 680 ppm on level 3
        and 620 ppm on level 4, so the limit really is ended by CO2 and really
        restarts at 700 ppm, over and over.
        """
        engine = self.make_engine(dry_hold_minutes=10)
        equilibrium = {1: 1200, 2: 950, 3: 680, 4: 620, 5: 560, 6: 520}
        co2, level, events, levels, counts = 650.0, 3, [], [], []
        active = None
        for i in range(3 * 3600 // TICK):
            now = T0 + i * TICK
            co2 += (equilibrium[level] - co2) * (1 - math.exp(-TICK / 1800))
            engine.update_measurements(rh=15, co2=co2, outdoor=-2)
            engine.set_external({"extract_temp": 21.5, "indoor_temperature_average": 22.0,
                                 "indoor_rh_average": 25.0, "max_room_co2": co2})
            result = engine.resolve(now)
            level = result["effective_level"]
            if result["dry_protection_active"] != active:
                active = result["dry_protection_active"]
                events.append((now, active, co2))
            levels.append(level)
            counts.append(result["level_changes_last_hour"])
        starts = [e for e in events if e[1]]
        stops = [e for e in events if not e[1]]
        # The limit both ends on CO2 and restarts: the cycle is exercised.
        self.assertGreaterEqual(len(starts), 3, events)
        self.assertGreaterEqual(len(stops), 2, events)
        for _at, _active, value in stops:
            self.assertGreater(value, 800, events)
        for (stop_at, _a, _v), (start_at, _b, value) in zip(stops, starts[1:]):
            # A restart needs CO2 down at 700 ppm and the hold time passed.
            self.assertLessEqual(value, 700, events)
            self.assertGreaterEqual(start_at - stop_at, 600, events)
        last_hour = levels[-3600 // TICK - 1:]
        changes = sum(1 for a, b in zip(last_hour, last_hour[1:]) if a != b)
        self.assertGreater(changes, 0, events)
        # Well under the flapping limit, and every one of them counted as automatic.
        self.assertLessEqual(changes, LEVEL_FLAP_CLEAR, events)
        self.assertEqual(counts[-1], changes)
        self.assertLess(max(counts), LEVEL_FLAP_LIMIT)

    def test_first_start_needs_only_co2_at_the_setpoint(self):
        engine = self.make_engine()
        engine.update_measurements(rh=15, co2=780, outdoor=-2)
        engine.set_external({"extract_temp": 21.5, "indoor_rh_average": 25, "max_room_co2": 790})
        result = engine.resolve(T0)
        self.assertTrue(result["dry_protection_active"])
        self.assertEqual(result["effective_level"], 2)

    def test_restart_after_a_co2_stop_waits_for_the_lower_limit(self):
        engine = self.make_engine(dry_hold_minutes=10)
        external = {"extract_temp": 21.5, "indoor_rh_average": 25}
        engine.update_measurements(rh=15, co2=600, outdoor=-2)
        engine.set_external(external)
        self.assertTrue(engine.resolve(T0)["dry_protection_active"])
        engine.update_measurements(co2=900)
        result = engine.resolve(T0 + 60)
        self.assertFalse(result["dry_protection_active"])
        self.assertEqual(result["dry_protection_state"], "co2_high")
        # Under the setpoint but over 700 ppm: waits, and says why.
        engine.update_measurements(co2=750)
        result = engine.resolve(T0 + 3600)
        self.assertFalse(result["dry_protection_active"])
        self.assertEqual(result["dry_protection_state"], "co2_wait")
        engine.update_measurements(co2=690)
        self.assertTrue(engine.resolve(T0 + 3605)["dry_protection_active"])
        # The next stop and restart start over from the same rule.
        engine.update_measurements(co2=810)
        self.assertFalse(engine.resolve(T0 + 3610 + 600)["dry_protection_active"])
        engine.update_measurements(co2=780)
        self.assertEqual(engine.resolve(T0 + 3610 + 1300)["dry_protection_state"], "co2_wait")

    def test_co2_over_the_setpoint_while_off_also_waits_for_the_lower_limit(self):
        engine = self.make_engine(dry_hold_minutes=10)
        engine.update_measurements(rh=15, co2=600, outdoor=-2)
        engine.set_external({"extract_temp": 21.5, "indoor_rh_average": 25})
        self.assertTrue(engine.resolve(T0)["dry_protection_active"])
        # The rooms get humid: the limit stops on the RH (not on CO2).
        engine.set_external({"extract_temp": 21.5, "indoor_rh_average": 34})
        self.assertFalse(engine.resolve(T0 + 600)["dry_protection_active"])
        self.assertFalse(engine.dry_co2_stopped)
        # CO2 goes over its setpoint while the limit is off.
        engine.update_measurements(co2=900)
        engine.resolve(T0 + 700)
        self.assertTrue(engine.dry_co2_stopped)
        # Dry again with CO2 under the setpoint but over 700 ppm: no restart yet.
        engine.set_external({"extract_temp": 21.5, "indoor_rh_average": 25})
        engine.update_measurements(co2=750)
        result = engine.resolve(T0 + 1800)
        self.assertFalse(result["dry_protection_active"])
        self.assertEqual(result["dry_protection_state"], "co2_wait")
        engine.update_measurements(co2=690)
        self.assertTrue(engine.resolve(T0 + 1805)["dry_protection_active"])
        self.assertFalse(engine.dry_co2_stopped)

    def test_rh_wobbling_at_the_limit_is_absorbed_by_the_hysteresis(self):
        engine = self.make_engine()
        changes, levels, result = self.run_hour(engine, rooms=None, co2_wobble=(600, 620),
                                                rooms_wobble=(29.5, 31.5))
        # Starts at once below 30 % and holds: 31.5 % stays under 30 + 3 % hysteresis.
        self.assertEqual(changes, 0, levels[:40])
        self.assertEqual(set(levels), {2})
        self.assertTrue(result["dry_protection_active"])
        self.assertEqual(result["effective_level"], 2)

    def test_stop_and_start_respect_the_hold_time(self):
        engine = self.make_engine(dry_hold_minutes=10)
        engine.update_measurements(rh=15, co2=600, outdoor=-2)
        engine.set_external({"extract_temp": 21.5, "indoor_rh_average": 25})
        self.assertTrue(engine.resolve(T0)["dry_protection_active"])
        # Rooms now humid: the decision is held for 10 minutes.
        engine.set_external({"extract_temp": 21.5, "indoor_rh_average": 40})
        result = engine.resolve(T0 + 300)
        self.assertTrue(result["dry_protection_active"])
        self.assertEqual(result["dry_protection_state"], "held")
        result = engine.resolve(T0 + 600)
        self.assertFalse(result["dry_protection_active"])
        # Dry again at once: waits for the hold.
        engine.set_external({"extract_temp": 21.5, "indoor_rh_average": 25})
        result = engine.resolve(T0 + 660)
        self.assertFalse(result["dry_protection_active"])
        self.assertEqual(result["dry_protection_state"], "waiting")
        self.assertTrue(engine.resolve(T0 + 1200)["dry_protection_active"])

    def test_high_co2_ends_the_limit_at_once(self):
        engine = self.make_engine()
        engine.update_measurements(rh=15, co2=600, outdoor=-2)
        engine.set_external({"extract_temp": 21.5, "indoor_rh_average": 25})
        self.assertTrue(engine.resolve(T0)["dry_protection_active"])
        engine.update_measurements(co2=1100)
        result = engine.resolve(T0 + 5)
        self.assertFalse(result["dry_protection_active"])
        self.assertEqual(result["dry_protection_state"], "co2_high")

    def test_bathroom_drying_releases_the_limit_at_once(self):
        engine = self.make_engine(mode="smart_auto", dry_hold_minutes=30)
        engine.update_measurements(rh=15, co2=500, outdoor=-2)
        engine.set_external({"extract_temp": 21.5, "indoor_rh_average": 25})
        engine.config.heartbeat("normal", requested_level=3, valid_for_s=180, reason="Normal")
        seen = engine.config.data["ha_last_seen"]
        result = engine.resolve(seen)
        self.assertTrue(result["dry_protection_active"])
        self.assertEqual(result["effective_level"], 2)
        # A shower: the bathrooms are not in the room RH, but drying them out wins.
        engine.config.heartbeat("boost", requested_level=4, valid_for_s=180,
                                reason="Badeværelse RH Bad 80%", bathroom_drying=True)
        seen = engine.config.data["ha_last_seen"]
        result = engine.resolve(seen)
        self.assertFalse(result["dry_protection_active"])
        self.assertEqual(result["dry_protection_state"], "humidity_demand")
        self.assertEqual(result["effective_level"], 4)
        self.assertEqual(result["effective_source"], "ha_smart")
        # Drying done: the limit returns, but only after its hold time.
        engine.config.heartbeat("normal", requested_level=3, valid_for_s=180, reason="Normal")
        seen = engine.config.data["ha_last_seen"]
        self.assertFalse(engine.resolve(seen + 60)["dry_protection_active"])
        self.assertEqual(engine.resolve(seen + 60)["dry_protection_state"], "waiting")

    def test_bathroom_drying_blocks_the_start(self):
        engine = self.make_engine(mode="smart_auto")
        engine.update_measurements(rh=15, co2=500, outdoor=-2)
        engine.set_external({"extract_temp": 21.5, "indoor_rh_average": 25})
        engine.config.heartbeat("boost", requested_level=4, valid_for_s=180,
                                reason="Badeværelse RH Bad 80%", bathroom_drying=True)
        result = engine.resolve(engine.config.data["ha_last_seen"])
        self.assertFalse(result["dry_protection_active"])
        self.assertEqual(result["effective_level"], 4)

    def test_ha_room_humidity_demand_releases_the_limit(self):
        engine = self.make_engine(mode="smart_auto")
        engine.update_measurements(rh=15, co2=500, outdoor=-2)
        engine.set_external({"extract_temp": 21.5, "indoor_rh_average": 25})
        engine.config.heartbeat("normal", requested_level=3, valid_for_s=180, reason="Normal")
        self.assertTrue(engine.resolve(engine.config.data["ha_last_seen"])["dry_protection_active"])
        engine.config.heartbeat("high", requested_level=4, valid_for_s=180,
                                reason="RH Køkken 60%", humidity_demand=True)
        result = engine.resolve(engine.config.data["ha_last_seen"])
        self.assertFalse(result["dry_protection_active"])
        self.assertEqual(result["dry_protection_state"], "humidity_demand")
        self.assertEqual(result["effective_level"], 4)

    def test_local_rh_demand_releases_the_limit(self):
        engine = self.make_engine()
        engine.update_measurements(rh=15, co2=500, outdoor=-2)
        engine.set_external({"extract_temp": 21.5, "indoor_rh_average": 25})
        self.assertTrue(engine.resolve(T0)["dry_protection_active"])
        # A shower makes the extract air humid; the dry living rooms must not block it.
        engine.update_measurements(rh=62)
        result = engine.resolve(T0 + 5)
        self.assertFalse(result["dry_protection_active"])
        self.assertEqual(result["dry_protection_state"], "humidity_demand")
        self.assertGreaterEqual(result["effective_level"], 4)

    def test_absolute_humidity_uses_the_ha_room_temperature(self):
        engine = self.make_engine()
        engine.update_measurements(rh=15, co2=500, outdoor=-2)
        engine.set_external({"extract_temp": 19.0, "afterheat_room_temperature": 19.0,
                             "indoor_temperature_average": 22.5, "indoor_rh_average": 40})
        # The HA rooms' RH at the rooms' own temperature, not the colder T3.
        self.assertAlmostEqual(engine.indoor_absolute_humidity(), absolute_humidity(22.5, 40), places=6)
        self.assertAlmostEqual(engine._room_absolute_humidity(25), absolute_humidity(22.5, 25), places=6)
        # Without a room temperature: the afterheat room temperature, then T3.
        engine.set_external({"extract_temp": 19.0, "afterheat_room_temperature": 21.0, "indoor_rh_average": 40})
        self.assertAlmostEqual(engine.indoor_absolute_humidity(), absolute_humidity(21.0, 40), places=6)
        # Stale HA rooms: the unit's own RH at the extract temperature.
        engine.set_external({"extract_temp": 19.0})
        self.assertAlmostEqual(engine.indoor_absolute_humidity(), absolute_humidity(19.0, 15), places=6)

    def test_lowest_room_basis(self):
        engine = self.make_engine(winter_rh_basis="lowest")
        engine.update_measurements(rh=45, co2=600, outdoor=-2)
        engine.set_external({"extract_temp": 21.5, "indoor_rh_average": 35, "indoor_rh_lowest": 28})
        result = engine.resolve(T0)
        self.assertTrue(result["dry_protection_active"])
        self.assertEqual(result["dry_protection_indoor_rh"], 28.0)

    def test_hold_setting_is_validated(self):
        engine = self.make_engine()
        self.assertEqual(engine.config.data["dry_hold_minutes"], 10)
        with self.assertRaises(ControllerError):
            engine.config.configure({"dry_hold_minutes": 0})
        engine.config.configure({"dry_hold_minutes": 20})
        self.assertEqual(engine.config.data["dry_hold_minutes"], 20)


class LevelChangeCounterTests(unittest.TestCase):
    def make_engine(self, **config):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        engine = ControllerEngine(ControllerState(Path(temp.name) / "controller.json"), HardwareAdapter())
        engine.config.configure(config)
        return engine

    def test_counts_automatic_changes_in_the_last_hour(self):
        # Level 4 is the top of four steps: its boost hold is kept short here.
        engine = self.make_engine(downshift_delay_seconds=60, boost_hold_seconds=60)
        engine.update_measurements(co2=500)
        self.assertEqual(engine.resolve(T0)["level_changes_last_hour"], 0)
        engine.resolve(T0 + 1)  # same level again: no change
        engine.update_measurements(co2=900)
        result = engine.resolve(T0 + 60)
        self.assertEqual(result["effective_level"], 4)
        self.assertEqual(result["level_changes_last_hour"], 1)
        self.assertEqual(result["level_change_quiet_seconds"], 0)
        engine.update_measurements(co2=500)
        # A step down needs the lower demand for the whole delay (from T0 + 140).
        self.assertEqual(engine.resolve(T0 + 140)["level_changes_last_hour"], 1)
        self.assertEqual(engine.resolve(T0 + 200)["level_changes_last_hour"], 2)
        self.assertEqual(engine.resolve(T0 + 500)["level_change_quiet_seconds"], 300)
        self.assertEqual(engine.resolve(T0 + 3660)["level_changes_last_hour"], 1)
        result = engine.resolve(T0 + 3800)
        self.assertEqual(result["level_changes_last_hour"], 0)
        self.assertIsNone(result["level_change_quiet_seconds"])

    def test_manual_and_overlay_changes_are_not_counted(self):
        engine = self.make_engine(mode="manual", manual_level=2)
        engine.resolve(T0)
        for i, level in enumerate((3, 2, 4, 2, 3), start=1):
            engine.config.configure({"manual_level": level})
            self.assertEqual(engine.resolve(T0 + 60 * i)["level_changes_last_hour"], 0)
        # Back to automatic: the switch itself is not counted either.
        engine.config.configure({"mode": "local_auto"})
        engine.update_measurements(co2=500)
        self.assertEqual(engine.resolve(T0 + 400)["level_changes_last_hour"], 0)
        # Quick boost, Køl, standby, bonfire and vacation, on and off.
        d = engine.config.data
        for i, (key, value) in enumerate((("quick_boost_until", T0 + 1000), ("cool_boost_until", T0 + 1000),
                                          ("bonfire_until", T0 + 1000), ("standby", True))):
            d[key] = value
            result = engine.resolve(T0 + 500 + 100 * i)
            self.assertTrue(any(result[flag] for flag in ("quick_boost_active", "cool_boost_active",
                                                          "bonfire_active", "standby_active")), key)
            d[key] = None if key != "standby" else False
            result = engine.resolve(T0 + 550 + 100 * i)
            self.assertEqual(result["effective_level"], 3, key)
            self.assertEqual(result["level_changes_last_hour"], 0, key)
        engine.config.configure({"vacation_enabled": True, "vacation_level": 1})
        engine.resolve(T0 + 1000)
        engine.config.configure({"vacation_enabled": False})
        self.assertEqual(engine.resolve(T0 + 1100)["level_changes_last_hour"], 0)

    def test_night_boundaries_are_not_counted(self):
        start = datetime.fromtimestamp(T0 + 600).astimezone().replace(second=0, microsecond=0)
        end = start + timedelta(minutes=30)
        engine = self.make_engine(night_enabled=True, night_level=2,
                                  night_start=start.strftime("%H:%M"), night_end=end.strftime("%H:%M"))
        engine.update_measurements(co2=500)
        self.assertEqual(engine.resolve(T0)["effective_level"], 3)
        result = engine.resolve(start.timestamp() + 30)
        self.assertEqual(result["effective_source"], "night")
        self.assertEqual(result["effective_level"], 2)
        self.assertEqual(result["level_changes_last_hour"], 0)
        result = engine.resolve(end.timestamp() + 30)
        self.assertEqual(result["effective_level"], 3)
        self.assertEqual(result["level_changes_last_hour"], 0)


class FlappingDiagnosticsTests(unittest.TestCase):
    @staticmethod
    def codes(result):
        return [a["code"] for a in result["diagnostics_alarms"]]

    def test_warning_above_twelve_changes_per_hour_with_hysteresis(self):
        d = Diagnostics()
        feed = {"rs485_healthy": True, "level_change_quiet_seconds": 30}
        r = d.update({**feed, "level_changes_last_hour": 12}, T0)
        self.assertNotIn("level_flapping", self.codes(r))
        r = d.update({**feed, "level_changes_last_hour": 13}, T0 + 10)
        alarms = {a["code"]: a for a in r["diagnostics_alarms"]}
        self.assertEqual(alarms["level_flapping"]["severity"], "warning")
        self.assertEqual(r["diagnostics_status"], "warning")
        # Around the limit it holds: 12, 10, 9 keep it, so it cannot flap itself.
        for i, count in enumerate((12, 10, 9, 13, 12), start=2):
            r = d.update({**feed, "level_changes_last_hour": count}, T0 + 10 * i)
            self.assertIn("level_flapping", self.codes(r), count)
        r = d.update({**feed, "level_changes_last_hour": LEVEL_FLAP_CLEAR}, T0 + 100)
        self.assertEqual(r["diagnostics_alarm_count"], 0)
        # Cleared: it needs more than 12 again.
        r = d.update({**feed, "level_changes_last_hour": 11}, T0 + 110)
        self.assertNotIn("level_flapping", self.codes(r))

    def test_quiet_period_clears_the_warning(self):
        d = Diagnostics()
        r = d.update({"rs485_healthy": True, "level_changes_last_hour": 14, "level_change_quiet_seconds": 5}, T0)
        self.assertIn("level_flapping", self.codes(r))
        r = d.update({"rs485_healthy": True, "level_changes_last_hour": 14,
                      "level_change_quiet_seconds": LEVEL_FLAP_QUIET_SECONDS}, T0 + LEVEL_FLAP_QUIET_SECONDS)
        self.assertNotIn("level_flapping", self.codes(r))
        # A burst that has gone quiet does not raise it again.
        r = d.update({"rs485_healthy": True, "level_changes_last_hour": 14,
                      "level_change_quiet_seconds": LEVEL_FLAP_QUIET_SECONDS + 10}, T0 + LEVEL_FLAP_QUIET_SECONDS + 10)
        self.assertNotIn("level_flapping", self.codes(r))

    def test_after_a_quiet_clear_one_change_does_not_raise_it_again(self):
        def clear():
            d = Diagnostics()
            self.assertIn("level_flapping", self.codes(d.update(
                {"rs485_healthy": True, "level_changes_last_hour": 14, "level_change_quiet_seconds": 5}, T0)))
            self.assertNotIn("level_flapping", self.codes(d.update(
                {"rs485_healthy": True, "level_changes_last_hour": 14,
                 "level_change_quiet_seconds": LEVEL_FLAP_QUIET_SECONDS}, T0 + LEVEL_FLAP_QUIET_SECONDS)))
            return d, T0 + LEVEL_FLAP_QUIET_SECONDS

        # One change after the clear: the old burst is still in the hour's count (15), but no warning.
        d, cleared = clear()
        r = d.update({"rs485_healthy": True, "level_changes_last_hour": 15, "level_change_quiet_seconds": 1},
                     cleared + 60)
        self.assertNotIn("level_flapping", self.codes(r))
        # A fresh burst (LEVEL_FLAP_LIMIT changes since the clear, that one included) raises it again.
        for i in range(1, LEVEL_FLAP_LIMIT - 1):
            r = d.update({"rs485_healthy": True, "level_changes_last_hour": 15 + i, "level_change_quiet_seconds": 1},
                         cleared + 60 + 10 * i)
            self.assertNotIn("level_flapping", self.codes(r), i)
        r = d.update({"rs485_healthy": True, "level_changes_last_hour": 14 + LEVEL_FLAP_LIMIT,
                      "level_change_quiet_seconds": 1}, cleared + 60 + 10 * LEVEL_FLAP_LIMIT)
        self.assertIn("level_flapping", self.codes(r))
        # Once the old burst has left the hour, more than the limit raises it as usual.
        d, cleared = clear()
        r = d.update({"rs485_healthy": True, "level_changes_last_hour": 13, "level_change_quiet_seconds": 1},
                     cleared + LEVEL_FLAP_REARM_SECONDS - 10)
        self.assertNotIn("level_flapping", self.codes(r))
        r = d.update({"rs485_healthy": True, "level_changes_last_hour": 13, "level_change_quiet_seconds": 11},
                     cleared + LEVEL_FLAP_REARM_SECONDS)
        self.assertIn("level_flapping", self.codes(r))
        self.assertGreaterEqual(LEVEL_FLAP_REARM_SECONDS, 30 * 60)


class DryAirRuntimeTests(unittest.TestCase):
    def test_stale_ha_rooms_turn_the_limit_off(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        runtime = ControllerRuntime(gateway_state={"humidity": 15, "co2": 600}, hardware=HardwareAdapter(),
                                    state_path=Path(temp.name) / "controller.json")
        runtime.hardware_writes_allowed = lambda: False
        runtime.config.configure({"dry_protection_enabled": True, "dry_rh_limit": 30, "dry_max_level": 2})
        runtime.room_inputs({"source": "home_assistant", "valid_for_s": 180, "rooms": {
            "Stue": {"humidity": 25}, "Kontor": {"humidity": 27}}})
        runtime.refresh_measurements()
        self.assertEqual(runtime.engine.ha_indoor_rh(), 26.0)
        runtime.smart_inputs_received_at -= 3600
        runtime.refresh_measurements()
        self.assertIsNone(runtime.engine.ha_indoor_rh())
        self.assertFalse(runtime.engine.resolve()["dry_protection_active"])

    def test_indoor_temperature_comes_from_the_humidity_rooms(self):
        # A room with control off (an unheated garage) is left out of the RH,
        # so its temperature is left out of the RH's absolute humidity too.
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        runtime = ControllerRuntime(gateway_state={"humidity": 15, "co2": 600}, hardware=HardwareAdapter(),
                                    state_path=Path(temp.name) / "controller.json")
        runtime.hardware_writes_allowed = lambda: False
        runtime.room_inputs({"source": "home_assistant", "valid_for_s": 180, "rooms": {
            "Stue": {"humidity": 40, "temperature": 22.0},
            "Kontor": {"humidity": 44, "temperature": 21.0},
            "Garage": {"humidity": 70, "temperature": 8.0, "control": False}}})
        runtime.refresh_measurements()
        self.assertEqual(runtime.engine.ha_indoor_rh(), 42.0)
        self.assertEqual(runtime.engine.external["indoor_temperature_average"], 21.5)
        self.assertAlmostEqual(runtime.engine.indoor_absolute_humidity(), absolute_humidity(21.5, 42.0), places=6)


if __name__ == "__main__":
    unittest.main()
